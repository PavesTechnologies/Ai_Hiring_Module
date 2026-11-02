"""
Phase B: fallback HOLDs get full scoring once quota is back. Applied only if no person acted on the
candidate since the fallback - otherwise recorded, and the stage left alone.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.models.async_tasks import TaskStatus
from app.models.pipeline import DecisionType, PipelineStage

MODULE = "app.tasks.full_scoring_tasks"


# --- the stage effect ------------------------------------------------------------------------

def _cc(stage, details=None):
    return SimpleNamespace(id=uuid4(), pipeline_stage=stage, decision_details=details)


CAPPED_SHORTLIST = {"capped_at_hold": True, "ai_recommendation": "SHORTLIST", "ai_response": {"score": 81}}


@pytest.mark.parametrize("stage, outcome, details, effect, rejected, moved_to", [
    (PipelineStage.HOLD, "REJECT", CAPPED_SHORTLIST, "rejected", True, None),
    (PipelineStage.SCREENING, "REJECT", None, "rejected", True, None),
    (PipelineStage.SHORTLISTED, "REJECT", None, "not rejected: candidate is in SHORTLISTED", False, None),
    (PipelineStage.HOLD, "PASS", CAPPED_SHORTLIST, "shortlisted (AI recommendation, review cap lifted)", False,
     PipelineStage.SHORTLISTED),
    (PipelineStage.HOLD, "PASS", {"capped_at_hold": True, "ai_recommendation": "HOLD"},
     "stays on HOLD (no capped AI shortlist to apply)", False, None),
    (PipelineStage.SCREENING, "PASS", None, "no stage change", False, None),     # carries on, uncapped
    (PipelineStage.HOLD, "REVIEW", CAPPED_SHORTLIST, "no stage change", False, None),
])
def test_the_full_outcome_applied_to_a_candidate_nobody_touched(stage, outcome, details, effect, rejected, moved_to):
    from app.tasks.full_scoring_tasks import apply_full_outcome
    stages = MagicMock()
    stages.transition_to_rejected.return_value = True
    stages.transition_on_ai_success.return_value = True
    full = {"outcome": outcome, "failed_checks": ["Core skills"], "mandatory_skills": [], "core_skill_count": 1,
            "core_confirmed_count": 0, "required_core_missing": [], "core_rejected_by_llm": []}
    with patch(f"{MODULE}.v2_rejection_reason", return_value="Missing core skills"):
        assert apply_full_outcome(stages, _cc(stage, details), full) == effect
    assert stages.transition_to_rejected.called is rejected
    if moved_to:
        kwargs = stages.transition_on_ai_success.call_args.kwargs
        assert kwargs["to_stage"] == moved_to and kwargs["decision_type"] == DecisionType.SHORTLISTED
    else:
        stages.transition_on_ai_success.assert_not_called()


# --- the task --------------------------------------------------------------------------------

class _World:
    def __init__(self, full_outcome="PASS", acted_by_person=None, awaiting=True):
        self.fallback = SimpleNamespace(id=uuid4(), awaiting_full_scoring=awaiting, acted_on=True,
                                        created_at=datetime.now(timezone.utc) - timedelta(hours=5))
        self.full_row = SimpleNamespace(acted_on=False, breakdown={"outcome": full_outcome})
        self.cc = SimpleNamespace(id=uuid4(), campaign_id=uuid4(), resume_id=uuid4(),
                                  pipeline_stage=PipelineStage.HOLD, decision_details=CAPPED_SHORTLIST,
                                  deterministic_breakdown={"awaiting_full_scoring": True, "needs_review": True})
        self.results = MagicMock()
        self.results.get.return_value = self.fallback
        self.results.latest.return_value = self.full_row
        self.candidates = MagicMock()
        self.candidates.get_by_id.return_value = self.cc
        self.service = MagicMock()
        self.service.score.return_value = {"outcome": full_outcome, "deterministic_score": 80.0,
                                           "deterministic_passed": full_outcome != "REJECT", "needs_review": False}
        self.apply = MagicMock(return_value="shortlisted (AI recommendation, review cap lifted)")
        self.audit = MagicMock()
        self.acted_by_person = acted_by_person

    def run(self):
        from app.tasks.full_scoring_tasks import complete_full_scoring_task
        logs = MagicMock()
        logs.get_by_task_id.return_value = None
        db = MagicMock()
        with patch(f"{MODULE}.SessionLocal", return_value=db), \
                patch(f"{MODULE}.CeleryTaskLogRepository", return_value=logs), \
                patch(f"{MODULE}.ScoringResultRepository", return_value=self.results), \
                patch(f"{MODULE}.CampaignCandidateRepository", return_value=self.candidates), \
                patch(f"{MODULE}.CampaignRepository"), patch(f"{MODULE}.ResumeRepository"), \
                patch(f"{MODULE}.JDRepository"), patch(f"{MODULE}.ConfigRepository"), \
                patch(f"{MODULE}._v2_service", return_value=self.service), \
                patch(f"{MODULE}.human_acted_since", return_value=self.acted_by_person), \
                patch(f"{MODULE}.apply_full_outcome", self.apply), \
                patch(f"{MODULE}.StageTransitionService"), patch(f"{MODULE}.AuditService", return_value=self.audit), \
                patch("app.tasks.deterministic_scoring_tasks.build_validation_inputs",
                      return_value=({}, {}, {}, {})):
            complete_full_scoring_task.run(campaign_candidate_id=str(self.cc.id),
                                           fallback_result_id=str(self.fallback.id))
        return db


def test_applied_when_nobody_acted_since_the_fallback():
    w = _World("PASS")
    db = w.run()
    assert w.service.score.call_args.kwargs["llm_fallback"] == "never"        # the full scoring, never the fallback
    assert w.service.score.call_args.kwargs["acted_on"] is False
    assert (w.full_row.acted_on, w.fallback.acted_on, w.fallback.awaiting_full_scoring) == (True, False, False)
    assert w.cc.deterministic_breakdown["awaiting_full_scoring"] is False
    w.apply.assert_called_once()
    assert w.full_row.breakdown["applied"] is True and w.full_row.breakdown["effect"].startswith("shortlisted")
    assert w.full_row.breakdown["completes_fallback_result_id"] == str(w.fallback.id)
    assert w.audit.log.call_args.kwargs["details"]["applied"] is True
    db.commit.assert_called()


def test_recorded_only_when_a_person_acted_since():
    w = _World("REJECT", acted_by_person="moved to SHORTLISTED by a person at ...")
    w.run()
    w.apply.assert_not_called()
    assert (w.full_row.acted_on, w.fallback.acted_on, w.fallback.awaiting_full_scoring) == (False, True, False)
    assert w.full_row.breakdown["applied"] is False and "a person acted" in w.full_row.breakdown["not_applied_reason"]
    # the decision and stage untouched; only the wait is over (no more badge)
    assert w.cc.deterministic_breakdown["awaiting_full_scoring"] is False
    assert w.cc.deterministic_breakdown["full_scoring_recorded_only"] is True
    assert w.cc.deterministic_breakdown["needs_review"] is True


@pytest.mark.parametrize("outcome", ["PENDING", "NOT_RUN"])
def test_still_no_llm_leaves_it_awaiting(outcome):
    w = _World(outcome)
    w.run()
    w.apply.assert_not_called()
    assert w.fallback.awaiting_full_scoring is True and w.fallback.acted_on is True


def test_nothing_awaiting_is_skipped():
    w = _World(awaiting=False)
    w.run()
    w.service.score.assert_not_called()


# --- the scan --------------------------------------------------------------------------------

def test_the_scan_queues_while_the_cap_has_room_and_skips_in_flight():
    from app.tasks import full_scoring_tasks as tasks
    rows = [SimpleNamespace(id=uuid4(), campaign_candidate_id=uuid4()) for _ in range(4)]
    logs = MagicMock()
    logs.get_by_task_id.return_value = None
    logs.get_by_campaign_candidate_and_task_type.side_effect = lambda cc_id, task_type: (
        [SimpleNamespace(status=TaskStatus.RUNNING)] if cc_id == rows[0].campaign_candidate_id else [])
    results = MagicMock()
    results.awaiting_full_scoring.return_value = rows
    service = MagicMock()
    service._budget.side_effect = lambda calls: calls <= tasks.CALLS_PER_CANDIDATE * 2   # room for two
    apply_async = MagicMock()
    with patch(f"{MODULE}.SessionLocal", return_value=MagicMock()), \
            patch(f"{MODULE}.CeleryTaskLogRepository", return_value=logs), \
            patch(f"{MODULE}.ScoringResultRepository", return_value=results), \
            patch(f"{MODULE}._v2_service", return_value=service), \
            patch.object(tasks.complete_full_scoring_task, "apply_async", apply_async):
        tasks.complete_full_scoring_scan()
    queued = [c.kwargs["kwargs"]["campaign_candidate_id"] for c in apply_async.call_args_list]
    assert queued == [str(rows[1].campaign_candidate_id), str(rows[2].campaign_candidate_id)]
    summary = logs.update.call_args.args[0].output_summary
    assert '"queued": 2' in summary and '"in_flight": 1' in summary and '"quota_reached": true' in summary


def test_the_scan_is_scheduled_hourly():
    from app.core.celery_app import celery_app
    assert celery_app.conf.beat_schedule["complete-full-scoring"]["task"] == "scoring.complete_full_scoring_scan"
