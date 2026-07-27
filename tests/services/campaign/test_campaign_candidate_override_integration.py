"""
M07-E03 S04 - integration coverage.

Exercises the real StageTransitionService.apply_hr_override (not mocked)
wired into CampaignCandidateService, and the full reject -> override
lifecycle across apply_hr_override + get_campaign_candidate_scorecard +
get_rejection_history together - matching this suite's existing
"integration test" convention (real collaborators, only the DB-facing
repositories mocked).
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.models.pipeline import PipelineStage, RejectionLayer, TransitionSource
from app.schemas.campaign.campaign_candidate_schema import CandidateScorecardResponse
from app.services.campaign.campaign_candidate_service import CampaignCandidateService
from app.services.campaign.stage_transition_service import StageTransitionService


def _make_campaign_candidate():
    return SimpleNamespace(
        id=uuid4(),
        campaign_id=uuid4(),
        candidate_id=uuid4(),
        resume_id=uuid4(),
        pipeline_stage=PipelineStage.REJECTED,
        score_breakdown={"deterministic_score": 40.0, "deterministic_passed": False},
        hr_override=False,
        hr_override_reason=None,
        hr_override_by=None,
        hr_override_at=None,
        deterministic_passed=False,
        ai_evaluation_status=None,
        deterministic_score=40.0,
        ai_ats_score=None,
        semantic_score=None,
        composite_score=None,
        created_at=datetime.now(timezone.utc),
    )


def _make_rejection(reason="Missing required skills: Python."):
    return SimpleNamespace(
        id=uuid4(),
        rejection_layer=RejectionLayer.DETERMINISTIC,
        rejection_reason=reason,
        rejected_at=datetime.now(timezone.utc),
    )


def test_apply_hr_override_with_real_stage_transition_service_moves_candidate_to_screening():
    candidate = _make_campaign_candidate()
    rejection = _make_rejection()

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate
    candidate_rejection_repo = MagicMock()
    candidate_rejection_repo.get_by_campaign_candidate_id.return_value = [rejection]

    allowed_transition_repo = MagicMock()
    allowed_transition_repo.is_transition_allowed.return_value = True
    stage_transition_service = StageTransitionService(allowed_transition_repo, campaign_candidate_repo)

    service = CampaignCandidateService(
        campaign_repo=MagicMock(),
        campaign_candidate_repo=campaign_candidate_repo,
        audit_service=MagicMock(),
        candidate_rejection_repo=candidate_rejection_repo,
        stage_transition_service=stage_transition_service,
    )

    result = service.apply_hr_override(candidate.id, "HR reviewed manually and disagrees with the rejection.", "hr-1", "HR_ADMIN")

    assert isinstance(result, CandidateScorecardResponse)
    assert candidate.pipeline_stage == PipelineStage.SCREENING
    allowed_transition_repo.is_transition_allowed.assert_called_once_with(
        PipelineStage.REJECTED, PipelineStage.SCREENING,
    )
    campaign_candidate_repo.create_stage_history.assert_called_once_with(
        campaign_candidate_id=candidate.id,
        from_stage=PipelineStage.REJECTED,
        to_stage=PipelineStage.SCREENING,
        changed_by="hr-1",
        change_reason="HR_ADMIN override of deterministic rejection",
        transition_source=TransitionSource.MANUAL,
        scores_snapshot=None,
    )

    # candidate_rejections is never deleted or mutated by the override.
    candidate_rejection_repo.create.assert_not_called()


def test_apply_hr_override_blocked_by_real_stage_transition_service_leaves_candidate_rejected():
    candidate = _make_campaign_candidate()
    rejection = _make_rejection()

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate
    candidate_rejection_repo = MagicMock()
    candidate_rejection_repo.get_by_campaign_candidate_id.return_value = [rejection]

    allowed_transition_repo = MagicMock()
    allowed_transition_repo.is_transition_allowed.return_value = False  # e.g. seed row removed/misconfigured
    stage_transition_service = StageTransitionService(allowed_transition_repo, campaign_candidate_repo)

    service = CampaignCandidateService(
        campaign_repo=MagicMock(),
        campaign_candidate_repo=campaign_candidate_repo,
        audit_service=MagicMock(),
        candidate_rejection_repo=candidate_rejection_repo,
        stage_transition_service=stage_transition_service,
    )

    import pytest
    from app.exceptions.campaign_exceptions import CampaignException

    with pytest.raises(CampaignException):
        service.apply_hr_override(candidate.id, "HR reviewed manually and disagrees with the rejection.", "hr-1", "HR_ADMIN")

    assert candidate.pipeline_stage == PipelineStage.REJECTED
    campaign_candidate_repo.create_stage_history.assert_not_called()


def test_reject_then_override_lifecycle_reflected_consistently_in_scorecard_and_history():
    """
    End-to-end across apply_hr_override + get_campaign_candidate_scorecard
    + get_rejection_history for the SAME candidate: the override must be
    visible consistently everywhere afterwards, and the original rejection
    must never be deleted or rewritten.
    """
    candidate = _make_campaign_candidate()
    rejection = _make_rejection(reason="Insufficient experience: 2 years provided, minimum 4 years required (gap: 2 years).")

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate
    candidate_rejection_repo = MagicMock()
    candidate_rejection_repo.get_by_campaign_candidate_id.return_value = [rejection]

    allowed_transition_repo = MagicMock()
    allowed_transition_repo.is_transition_allowed.return_value = True
    stage_transition_service = StageTransitionService(allowed_transition_repo, campaign_candidate_repo)

    service = CampaignCandidateService(
        campaign_repo=MagicMock(),
        campaign_candidate_repo=campaign_candidate_repo,
        audit_service=MagicMock(),
        candidate_rejection_repo=candidate_rejection_repo,
        stage_transition_service=stage_transition_service,
    )

    service.apply_hr_override(candidate.id, "HR reviewed manually and disagrees with the rejection.", "hr-1", "HR_ADMIN")

    scorecard = service.get_campaign_candidate_scorecard(candidate.id)
    history = service.get_rejection_history(candidate.id)

    assert scorecard.is_overridden is True
    assert scorecard.status == "Overridden — Previously Rejected"
    assert scorecard.rejection_reason == rejection.rejection_reason
    assert scorecard.has_rejection is False  # no longer currently in the REJECTED stage

    assert len(history) == 1
    assert history[0].rejection_reason == rejection.rejection_reason
    assert history[0].hr_override is True
    assert history[0].current_status is True
