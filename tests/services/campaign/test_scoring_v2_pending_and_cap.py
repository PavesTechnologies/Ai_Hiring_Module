"""
Scoring v2: a quota / provider failure gives PENDING (retried, dead-lettered when
retries run out) - never REJECT; the daily LLM cap gives NOT_RUN; v1 is never touched.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.services.campaign.scoring_v2 import (
    OUTCOME_NOT_RUN, OUTCOME_PENDING, EvidenceInput, JDSkillInput, ScoringInputs, apply_unavailable_outcome,
    merge_config, score_v2,
)
from app.services.llm.base import LLMErrorReason, LLMPermanentError, LLMTransientError
from app.services.skills.core_skill_confirmation_service import CoreSkillConfirmationService, CoreSkillSpec

AT = uuid4()


class Provider:
    provider_name, model = "GOOGLE", "m"
    last_model_used, last_used_fallback = None, False

    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0

    def generate_json(self, prompt, schema, *, deterministic=False):
        self.calls += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _confirm(provider, **kw):
    evidence = [{"canonical_skill_id": AT, "sense": None, "sources": ["DERIVED"],
                 "evidence": [{"text": "AI & Digital Transformation", "source": "DERIVED", "section": "SKILLS"}]}]
    return CoreSkillConfirmationService(provider).confirm(
        resume_id=uuid4(), jd_id=uuid4(), core_skills=[CoreSkillSpec(AT, "AI Transformation")],
        evidence_rows=evidence, text_hash="h", **kw)


QUOTA = LLMPermanentError("quota", reason=LLMErrorReason.DAILY_LIMIT)
DOWN = LLMTransientError("down", reason=LLMErrorReason.UNAVAILABLE)
YES = {"items": [{"number": 1, "present": True, "quote": "AI & Digital Transformation"}]}


@pytest.mark.parametrize("failures", [[QUOTA] * 3, [DOWN] * 3, [YES, QUOTA, YES], [DOWN, YES, YES]])
def test_any_lost_vote_marks_the_confirmation_unavailable(failures):
    outcome = _confirm(Provider(failures))
    assert outcome.llm_unavailable is True


def test_answered_votes_are_not_unavailable():
    assert _confirm(Provider([YES, YES, YES])).llm_unavailable is False


def test_budget_refusal_asks_nothing():
    provider = Provider([])
    outcome = _confirm(provider, budget=lambda n: False)
    assert outcome.skipped_budget is True and provider.calls == 0


def test_budget_is_asked_for_the_three_votes():
    asked = []
    _confirm(Provider([YES, YES, YES]), budget=lambda n: asked.append(n) or True)
    assert asked == [3]


def _rejecting_breakdown():
    jd = [JDSkillInput(AT, "AI Transformation", True, 1.0, "core")]
    result = score_v2(ScoringInputs(jd_skills=jd, evidence=[EvidenceInput(AT, None, ("DERIVED",), "DERIVED", "DERIVED", 0.8)],
                                    confirmations={}, hierarchy={}, allowed_missing_core=0), merge_config(None))
    assert result["outcome"] == "REJECT"   # what the score alone would say
    return result


def test_quota_failure_is_pending_never_reject():
    result = apply_unavailable_outcome(_rejecting_breakdown(), OUTCOME_PENDING, "DAILY_LIMIT: quota")
    assert result["outcome"] == OUTCOME_PENDING
    assert result["deterministic_passed"] is None and result["would_be_review"] is False
    assert result["provisional_score"] is not None


def test_daily_cap_is_not_run_without_a_score():
    result = apply_unavailable_outcome(_rejecting_breakdown(), OUTCOME_NOT_RUN, "daily LLM call cap reached")
    assert result["outcome"] == OUTCOME_NOT_RUN and result["deterministic_score"] is None
    assert result["deterministic_passed"] is None


# --- the shadow task: PENDING retries, then the failure queue ---------------

def _run_task(outcome, *, retries):
    import app.tasks.scoring_v2_tasks as tasks
    task = tasks.calculate_scoring_v2_task
    campaign = SimpleNamespace(id=uuid4(), jd_id=uuid4(), scoring_v2_enabled=False, scoring_v2_shadow=True)
    cc = SimpleNamespace(id=uuid4(), campaign_id=campaign.id, resume_id=uuid4())
    dlq = MagicMock()
    service = MagicMock()
    service.return_value.score.return_value = {"outcome": outcome, "outcome_reason": "DAILY_LIMIT", "confirmation": {}}
    with patch.object(tasks, "SessionLocal", return_value=MagicMock()), \
            patch.object(tasks, "CeleryTaskLogService") as log_service, \
            patch.object(tasks, "CampaignCandidateRepository") as cc_repo, \
            patch.object(tasks, "CampaignRepository") as campaign_repo, \
            patch.object(tasks, "ResumeRepository"), patch.object(tasks, "JDRepository"), \
            patch.object(tasks, "ConfigRepository"), patch.object(tasks, "SkillRepository"), \
            patch.object(tasks, "ScoringV2Service", service), \
            patch.object(tasks, "DeadLetterQueueRepository", return_value=dlq), \
            patch("app.tasks.deterministic_scoring_tasks.build_validation_inputs", return_value=({}, {}, {}, {})), \
            patch.object(task, "retry", side_effect=RuntimeError("retry")) as retry:
        log_service.return_value.repository.get_by_task_id.return_value = None
        cc_repo.return_value.get_by_id.return_value = cc
        campaign_repo.return_value.get_by_id.return_value = campaign
        task.push_request(id="t1", retries=retries)
        try:
            try:
                task.run(campaign_candidate_id=str(cc.id))
            except RuntimeError:
                pass
        finally:
            task.pop_request()
    return retry, dlq, log_service


def test_pending_is_retried():
    retry, dlq, _ = _run_task(OUTCOME_PENDING, retries=0)
    retry.assert_called_once()
    dlq.create.assert_not_called()


def test_pending_after_the_last_retry_goes_to_the_failure_queue():
    retry, dlq, log_service = _run_task(OUTCOME_PENDING, retries=3)
    retry.assert_not_called()
    assert dlq.create.call_args.kwargs["task_type"] == "SCORING_V2"
    log_service.return_value.mark_dead.assert_called_once()


def test_a_decided_outcome_succeeds_without_retry():
    retry, dlq, log_service = _run_task("REJECT", retries=0)
    retry.assert_not_called()
    dlq.create.assert_not_called()
    log_service.return_value.mark_success.assert_called_once()


def test_v2_dead_letters_are_replayable_from_the_existing_queue():
    from app.services.campaign.campaign_service import CampaignService
    from app.services.campaign.layer_failure import REPLAYABLE_TASK_TYPES
    assert "SCORING_V2" in CampaignService._DLQ_REPLAY_BUILDERS and "SCORING_V2" in REPLAYABLE_TASK_TYPES
