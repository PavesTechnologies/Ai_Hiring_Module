"""
M07-E03 S04 - integration coverage.

Exercises the real StageTransitionService.apply_hr_override (not mocked)
wired into CampaignCandidateService, and the full reject -> override
lifecycle across apply_hr_override + get_campaign_candidate_scorecard +
get_rejection_history together - real collaborators, only the DB-facing
repositories mocked.

Since the decision_* redesign (98d721e) the rejection being overridden is the
candidate's decision_* record (plus its REJECTED stage-history row); the real
transition replaces it with a RESET decision and keeps it in decision_details.
No Celery task is enqueued: the AI-evaluation apply_async and the semantic
enqueue are patched for every test.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.exceptions.campaign_exceptions import CampaignException
from app.models.pipeline import DecisionSource, DecisionType, PipelineStage, TransitionSource
from app.schemas.campaign.campaign_candidate_schema import CandidateScorecardResponse
from app.services.campaign.campaign_candidate_service import CampaignCandidateService
from app.services.campaign.stage_transition_service import StageTransitionService

_OVERRIDE_REASON = "HR reviewed manually and disagrees with the rejection."


@pytest.fixture(autouse=True)
def no_task_enqueue():
    with patch("app.tasks.ai_evaluation_tasks.calculate_ai_evaluation_task.apply_async"), \
            patch("app.services.campaign.campaign_candidate_service._enqueue_semantic_scoring"):
        yield


def _make_rejected_candidate(reason="Missing required skills: Python."):
    rejected_at = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid4(),
        campaign_id=uuid4(),
        candidate_id=uuid4(),
        resume_id=uuid4(),
        pipeline_stage=PipelineStage.REJECTED,
        previous_stage=PipelineStage.SCREENING,
        deterministic_breakdown={"deterministic_score": 40.0, "deterministic_passed": False},
        semantic_breakdown=None, semantic_passed=None,
        decision_type=DecisionType.REJECTED,
        decision_source=DecisionSource.DETERMINISTIC,
        decision_reason=reason,
        decision_details=None,
        decision_by_user_id=None,
        decision_at=rejected_at,
        ai_evaluation=None,
        deterministic_passed=False,
        deterministic_score=40.0,
        semantic_score=None,
        composite_score=None,
        created_at=rejected_at,
    )


def _rejection_row(candidate):
    """The REJECTED stage-history row the deterministic rejection wrote."""
    return SimpleNamespace(id=uuid4(), to_stage=PipelineStage.REJECTED, changed_at=candidate.decision_at,
                           scores_snapshot={"decision_source": candidate.decision_source.value,
                                            "decision_reason": candidate.decision_reason})


def _wire(candidate, transition_allowed=True):
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate
    campaign_candidate_repo.get_stage_history_by_campaign_candidate_id.return_value = [_rejection_row(candidate)]
    allowed_transition_repo = MagicMock()
    allowed_transition_repo.is_transition_allowed.return_value = transition_allowed
    stage_transition_service = StageTransitionService(
        allowed_transition_repo, campaign_candidate_repo, MagicMock(), MagicMock(),
    )
    service = CampaignCandidateService(
        campaign_repo=MagicMock(),
        campaign_candidate_repo=campaign_candidate_repo,
        audit_service=MagicMock(),
        stage_transition_service=stage_transition_service,
        ai_evaluation_repo=MagicMock(),
    )
    return service, campaign_candidate_repo, allowed_transition_repo


def test_apply_hr_override_with_real_stage_transition_service_moves_candidate_to_screening():
    candidate = _make_rejected_candidate()
    original_reason, original_at = candidate.decision_reason, candidate.decision_at
    service, campaign_candidate_repo, allowed_transition_repo = _wire(candidate)

    result = service.apply_hr_override(candidate.id, _OVERRIDE_REASON, "hr-1", "HR_ADMIN")

    assert isinstance(result, CandidateScorecardResponse)
    assert candidate.pipeline_stage == PipelineStage.SCREENING
    allowed_transition_repo.is_transition_allowed.assert_called_once_with(
        PipelineStage.REJECTED, PipelineStage.SCREENING, previous_stage=PipelineStage.SCREENING,
    )
    # The RESET decision replaces the rejection, which is kept - never deleted - in decision_details.
    assert candidate.decision_type == DecisionType.RESET
    assert candidate.decision_reason == _OVERRIDE_REASON
    assert candidate.decision_by_user_id == "hr-1"
    assert candidate.decision_details["overridden_decision_source"] == "DETERMINISTIC"
    assert candidate.decision_details["overridden_decision_reason"] == original_reason
    assert candidate.decision_details["overridden_decision_at"] == original_at.isoformat()

    campaign_candidate_repo.create_stage_history.assert_called_once()
    history_kwargs = campaign_candidate_repo.create_stage_history.call_args.kwargs
    assert (history_kwargs["from_stage"], history_kwargs["to_stage"]) == (PipelineStage.REJECTED, PipelineStage.SCREENING)
    assert history_kwargs["changed_by"] == "hr-1"
    assert history_kwargs["transition_source"] == TransitionSource.MANUAL
    assert history_kwargs["scores_snapshot"]["decision_type"] == DecisionType.RESET.value
    assert history_kwargs["scores_snapshot"]["decision_details"]["overridden_decision_reason"] == original_reason


def test_apply_hr_override_blocked_by_real_stage_transition_service_leaves_candidate_rejected():
    candidate = _make_rejected_candidate()
    service, campaign_candidate_repo, _ = _wire(candidate, transition_allowed=False)  # e.g. seed row removed

    with pytest.raises(CampaignException):
        service.apply_hr_override(candidate.id, _OVERRIDE_REASON, "hr-1", "HR_ADMIN")

    assert candidate.pipeline_stage == PipelineStage.REJECTED
    assert candidate.decision_type == DecisionType.REJECTED
    campaign_candidate_repo.create_stage_history.assert_not_called()
    campaign_candidate_repo.commit.assert_not_called()


def test_reject_then_override_lifecycle_reflected_consistently_in_scorecard_and_history():
    """
    End-to-end across apply_hr_override + get_campaign_candidate_scorecard
    + get_rejection_history for the SAME candidate: the override must be
    visible consistently everywhere afterwards, and the original rejection
    must never be deleted or rewritten.
    """
    candidate = _make_rejected_candidate(
        reason="Insufficient experience: 2 years provided, minimum 4 years required (gap: 2 years).")
    original_reason = candidate.decision_reason
    service, _, _ = _wire(candidate)

    service.apply_hr_override(candidate.id, _OVERRIDE_REASON, "hr-1", "HR_ADMIN")

    scorecard = service.get_campaign_candidate_scorecard(candidate.id)
    history = service.get_rejection_history(candidate.id)

    assert scorecard.is_overridden is True
    assert scorecard.status == "Overridden — Previously Rejected"
    assert scorecard.rejection_reason == original_reason
    assert scorecard.has_rejection is False  # no longer currently in the REJECTED stage

    assert len(history) == 1
    assert history[0].rejection_reason == original_reason
    assert history[0].hr_override is True
    assert history[0].current_status is True
