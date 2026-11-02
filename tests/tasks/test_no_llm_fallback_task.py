"""
Phase B on the deciding path: the daily cap falls back to no-LLM scoring at once; a provider failure
is retried and falls back only on the last attempt. A fallback HOLD carries on (semantic / AI), and
the stored breakdown carries the "awaiting full scoring" marker.
"""
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.models.pipeline import PipelineStage
from app.services.campaign.candidate_scoring_service import MandatorySkillMatchType
from app.services.document_processing.retry_policy import RetryPolicy
from tests.tasks.test_deterministic_scoring_tasks import (
    TASKS_MODULE, _breakdown, _Harness, _make_campaign, _make_campaign_candidate, _make_resume, _skill_entry,
)


def _fallback_hold():
    return {"outcome": "REVIEW", "deterministic_passed": True, "deterministic_score": 55.0,
            "needs_review": True, "needs_review_reason": "Awaiting full scoring (daily LLM call cap reached): ...",
            "near_miss_outcome": "HOLD", "near_misses": ["1 core skill not confirmed without the LLM (Python)"],
            "near_miss_reject_reasons": [], "scoring_mode": "no_llm", "awaiting_full_scoring": True,
            "fallback_reason": "daily LLM call cap reached"}


class _Retry(Exception):
    pass


def _run(h, v2_result, *, retries=0):
    from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task
    campaign = _make_campaign()
    campaign.scoring_v2_enabled = True
    cc = _make_campaign_candidate(campaign.id, uuid4())
    h.campaign_candidate_repo.get_by_id.return_value = cc
    h.campaign_repo.get_by_id.return_value = campaign
    h.resume_repo.get_by_id.return_value = _make_resume()
    h.scoring_service_instance.calculate_and_store_score_breakdown.return_value = _breakdown(
        [_skill_entry(uuid4(), MandatorySkillMatchType.MISSING.value)], 0.0, False)
    run_v2 = MagicMock(return_value=v2_result)
    retry = MagicMock(side_effect=_Retry())
    request = MagicMock(retries=retries, id=str(uuid4()))
    calculate_deterministic_score_task.push_request(retries=retries, id=request.id)
    try:
        with patch(f"{TASKS_MODULE}._run_scoring_v2", run_v2), \
                patch.object(calculate_deterministic_score_task, "retry", retry), \
                patch(f"{TASKS_MODULE}._DECISION_PENDING_RETRY_POLICY",
                      RetryPolicy(max_attempts=4, base_delay_seconds=120, max_delay_seconds=1800)):
            try:
                calculate_deterministic_score_task.run(campaign_candidate_id=str(cc.id))
            except _Retry:
                pass
    finally:
        calculate_deterministic_score_task.pop_request()
    return cc, run_v2, retry


@pytest.mark.parametrize("retries, mode", [(0, "on_cap"), (2, "on_cap"), (3, "on_unavailable")])
def test_the_fallback_allowed_depends_on_the_attempt(retries, mode):
    with _Harness() as h:
        _, run_v2, _ = _run(h, _fallback_hold(), retries=retries)
        assert run_v2.call_args.kwargs["llm_fallback"] == mode


def test_a_fallback_hold_carries_on_with_the_marker_and_is_never_rejected():
    with _Harness() as h:
        cc, _, retry = _run(h, _fallback_hold())
        retry.assert_not_called()
        assert cc.pipeline_stage != PipelineStage.REJECTED
        h.queue_rejection_email_mock.assert_not_called()
        h.enqueue_semantic_scoring_mock.assert_called_once()            # on to semantic / AI, capped at HOLD there
        stored = cc.deterministic_breakdown
        assert stored["awaiting_full_scoring"] is True and stored["scoring_mode"] == "no_llm"
        assert stored["needs_review"] is True and stored["needs_review_reason"].startswith("Awaiting full scoring")
