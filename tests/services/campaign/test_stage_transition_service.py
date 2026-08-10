from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.models.pipeline import DecisionSource, PipelineStage, TransitionSource
from app.services.campaign.stage_transition_service import StageTransitionService

"""
M07-E03 S02 T01: StageTransitionService.transition_to_rejected - validates
against allowed_transitions before ever writing pipeline_stage or
campaign_candidate_stage_history, so a missing/blocked transition is a
clean no-op, never a partial write.
"""


def _make_candidate(pipeline_stage=PipelineStage.SCREENING):
    # decision_* fields default to None here, matching a real CampaignCandidate
    # row that has never had a decision recorded yet - transition_to_rejected/
    # apply_hr_override both read these (apply_hr_override reads them before
    # this fixture existed, to snapshot the decision being overridden).
    return SimpleNamespace(
        id=uuid4(),
        pipeline_stage=pipeline_stage,
        decision_type=None,
        decision_source=None,
        decision_reason=None,
        decision_details=None,
        decision_by_user_id=None,
        decision_at=None,
    )


def make_service(is_allowed: bool):
    allowed_transition_repo = MagicMock()
    allowed_transition_repo.is_transition_allowed.return_value = is_allowed
    campaign_candidate_repo = MagicMock()
    service = StageTransitionService(allowed_transition_repo, campaign_candidate_repo, MagicMock())
    return service, allowed_transition_repo, campaign_candidate_repo


def test_transition_applies_when_allowed():
    candidate = _make_candidate(pipeline_stage=PipelineStage.SCREENING)
    service, allowed_transition_repo, campaign_candidate_repo = make_service(is_allowed=True)
    snapshot = {"deterministic_score": 40.0}

    result = service.transition_to_rejected(
        candidate,
        change_reason="Deterministic filter rejection",
        scores_snapshot=snapshot,
        decision_source=DecisionSource.DETERMINISTIC,
    )

    assert result is True
    assert candidate.pipeline_stage == PipelineStage.REJECTED
    allowed_transition_repo.is_transition_allowed.assert_called_once_with(
        PipelineStage.SCREENING, PipelineStage.REJECTED,
    )
    campaign_candidate_repo.update.assert_called_once_with(candidate)
    campaign_candidate_repo.create_stage_history.assert_called_once_with(
        campaign_candidate_id=candidate.id,
        from_stage=PipelineStage.SCREENING,
        to_stage=PipelineStage.REJECTED,
        changed_by=None,
        change_reason="Deterministic filter rejection",
        transition_source=TransitionSource.SYSTEM,
        scores_snapshot={
            **snapshot,
            "decision_type": "REJECTED",
            "decision_source": "DETERMINISTIC",
            "decision_reason": "Deterministic filter rejection",
            "decision_details": None,
        },
    )


def test_transition_is_a_no_op_when_blocked():
    candidate = _make_candidate(pipeline_stage=PipelineStage.SCREENING)
    service, allowed_transition_repo, campaign_candidate_repo = make_service(is_allowed=False)

    result = service.transition_to_rejected(
        candidate,
        change_reason="Deterministic filter rejection",
        scores_snapshot={},
        decision_source=DecisionSource.DETERMINISTIC,
    )

    assert result is False
    # pipeline_stage must be untouched - still SCREENING, not silently REJECTED.
    assert candidate.pipeline_stage == PipelineStage.SCREENING
    campaign_candidate_repo.update.assert_not_called()
    campaign_candidate_repo.create_stage_history.assert_not_called()


"""
M07-E03 S04 T02: StageTransitionService.apply_hr_override - same
validate-then-apply shape as transition_to_rejected, but REJECTED ->
SCREENING, MANUAL/HR_ADMIN-attributed instead of SYSTEM/anonymous.
"""


def test_apply_hr_override_applies_when_allowed():
    candidate = _make_candidate(pipeline_stage=PipelineStage.REJECTED)
    service, allowed_transition_repo, campaign_candidate_repo = make_service(is_allowed=True)

    result = service.apply_hr_override(
        candidate, changed_by="hr-admin-1", change_reason="HR_ADMIN override of deterministic rejection",
    )

    assert result is True
    assert candidate.pipeline_stage == PipelineStage.SCREENING
    allowed_transition_repo.is_transition_allowed.assert_called_once_with(
        PipelineStage.REJECTED, PipelineStage.SCREENING,
    )
    campaign_candidate_repo.update.assert_called_once_with(candidate)
    campaign_candidate_repo.create_stage_history.assert_called_once_with(
        campaign_candidate_id=candidate.id,
        from_stage=PipelineStage.REJECTED,
        to_stage=PipelineStage.SCREENING,
        changed_by="hr-admin-1",
        change_reason="HR_ADMIN override of deterministic rejection",
        transition_source=TransitionSource.MANUAL,
        scores_snapshot={
            "decision_type": "RESET",
            "decision_source": "HR_ADMIN",
            "decision_reason": "HR_ADMIN override of deterministic rejection",
            "decision_details": {
                "overridden_decision_type": None,
                "overridden_decision_source": None,
                "overridden_decision_reason": None,
                "overridden_decision_at": None,
            },
        },
    )


def test_apply_hr_override_is_a_no_op_when_blocked():
    candidate = _make_candidate(pipeline_stage=PipelineStage.REJECTED)
    service, allowed_transition_repo, campaign_candidate_repo = make_service(is_allowed=False)

    result = service.apply_hr_override(
        candidate, changed_by="hr-admin-1", change_reason="HR_ADMIN override of deterministic rejection",
    )

    assert result is False
    # pipeline_stage must be untouched - still REJECTED, not silently SCREENING.
    assert candidate.pipeline_stage == PipelineStage.REJECTED
    campaign_candidate_repo.update.assert_not_called()
    campaign_candidate_repo.create_stage_history.assert_not_called()
