from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.exceptions.campaign_exceptions import CampaignException
from app.exceptions.pipeline_transition_exceptions import (
    InvalidPipelineTransitionException,
    PipelineTransitionReasonRequiredException,
)
from app.models.pipeline import PipelineStage, TransitionSource
from app.schemas.campaign.campaign_candidate_schema import CampaignCandidateResponse
from app.services.campaign.campaign_candidate_service import CampaignCandidateService

"""
Pipeline Board - GET .../board (get_campaign_board) and the drag-and-drop
POST .../stage endpoint (move_pipeline_stage). Both reuse existing,
already-tested logic verbatim: get_campaign_board buckets
get_campaign_candidates' own output (no new query/mapping);
move_pipeline_stage delegates entirely to PipelineTransitionService (no new
transition rules). These tests verify only the new orchestration.
"""

_BOARD_STAGES = [
    PipelineStage.UPLOADED, PipelineStage.SCREENING, PipelineStage.SHORTLISTED,
    PipelineStage.HOLD, PipelineStage.INTERVIEW, PipelineStage.SELECTED, PipelineStage.REJECTED,
]


def make_service(**overrides):
    defaults = dict(campaign_repo=MagicMock(), campaign_candidate_repo=MagicMock(), audit_service=MagicMock())
    defaults.update(overrides)
    return CampaignCandidateService(**defaults)


def _candidate_response(pipeline_stage, **overrides):
    defaults = dict(
        id=uuid4(), campaign_id=uuid4(), candidate_id=uuid4(), campaign_candidate_id=uuid4(),
        resume_id=uuid4(), pipeline_stage=pipeline_stage, created_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return CampaignCandidateResponse(**defaults)


def _campaign_candidate(pipeline_stage=PipelineStage.SCREENING):
    return SimpleNamespace(
        id=uuid4(), campaign_id=uuid4(), candidate_id=uuid4(), resume_id=uuid4(),
        pipeline_stage=pipeline_stage, created_at=datetime.now(timezone.utc),
        deterministic_score=None, semantic_score=None, composite_score=None,
        is_fraud_flagged=False, decision_type=None,
    )


# ----------------------------------------------------------------------
# get_campaign_board
# ----------------------------------------------------------------------

def test_get_campaign_board_reuses_get_campaign_candidates():
    service = make_service()
    with patch.object(service, "get_campaign_candidates", return_value=[]) as mock_get:
        service.get_campaign_board(uuid4())
    mock_get.assert_called_once()


def test_get_campaign_board_buckets_by_stage():
    service = make_service()
    items = [
        _candidate_response(PipelineStage.UPLOADED),
        _candidate_response(PipelineStage.SCREENING),
        _candidate_response(PipelineStage.SCREENING),
        _candidate_response(PipelineStage.SELECTED),
    ]
    with patch.object(service, "get_campaign_candidates", return_value=items):
        result = service.get_campaign_board(uuid4())

    by_stage = {col.stage: col for col in result.columns}
    assert by_stage[PipelineStage.UPLOADED].count == 1
    assert by_stage[PipelineStage.SCREENING].count == 2
    assert by_stage[PipelineStage.SELECTED].count == 1
    assert by_stage[PipelineStage.SHORTLISTED].count == 0
    assert result.other_count == 0


def test_get_campaign_board_includes_all_seven_columns_even_when_empty():
    service = make_service()
    with patch.object(service, "get_campaign_candidates", return_value=[]):
        result = service.get_campaign_board(uuid4())

    assert [col.stage for col in result.columns] == _BOARD_STAGES
    assert all(col.count == 0 and col.candidates == [] for col in result.columns)


def test_get_campaign_board_counts_hm_review_and_fraud_review_as_other():
    """HM_REVIEW/FRAUD_REVIEW aren't board columns - candidates in them must still be accounted for, not silently dropped."""
    service = make_service()
    items = [
        _candidate_response(PipelineStage.HM_REVIEW),
        _candidate_response(PipelineStage.FRAUD_REVIEW),
        _candidate_response(PipelineStage.SCREENING),
    ]
    with patch.object(service, "get_campaign_candidates", return_value=items):
        result = service.get_campaign_board(uuid4())

    assert result.other_count == 2
    assert sum(col.count for col in result.columns) == 1


def test_get_campaign_board_response_carries_campaign_id():
    service = make_service()
    campaign_id = uuid4()
    with patch.object(service, "get_campaign_candidates", return_value=[]):
        result = service.get_campaign_board(campaign_id)

    assert result.campaign_id == campaign_id


# ----------------------------------------------------------------------
# move_pipeline_stage
# ----------------------------------------------------------------------

def test_move_pipeline_stage_raises_not_found_when_candidate_missing():
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = None
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    with pytest.raises(CampaignException) as exc_info:
        service.move_pipeline_stage(uuid4(), PipelineStage.SHORTLISTED, actor_id="user-1")

    assert exc_info.value.status_code == 404


def test_move_pipeline_stage_delegates_to_pipeline_transition_service():
    cc = _campaign_candidate()
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    pipeline_transition_service = MagicMock()
    candidate_repo = MagicMock()
    candidate_repo.get_by_id.return_value = None
    resume_repo = MagicMock()
    resume_repo.get_by_id.return_value = None
    service = make_service(
        campaign_candidate_repo=campaign_candidate_repo,
        pipeline_transition_service=pipeline_transition_service,
        candidate_repo=candidate_repo, resume_repo=resume_repo,
    )

    service.move_pipeline_stage(
        cc.id, PipelineStage.SHORTLISTED, actor_id="user-1", actor_role="HR_ADMIN", reason="promoted",
    )

    pipeline_transition_service.transition_stage.assert_called_once_with(
        cc, to_stage=PipelineStage.SHORTLISTED, changed_by="user-1", actor_role="HR_ADMIN",
        reason="promoted", source=TransitionSource.MANUAL,
    )
    campaign_candidate_repo.commit.assert_called_once()


def test_move_pipeline_stage_rolls_back_and_raises_409_on_invalid_transition():
    cc = _campaign_candidate()
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    pipeline_transition_service = MagicMock()
    pipeline_transition_service.transition_stage.side_effect = InvalidPipelineTransitionException(
        "SCREENING", "SELECTED",
    )
    service = make_service(
        campaign_candidate_repo=campaign_candidate_repo, pipeline_transition_service=pipeline_transition_service,
    )

    with pytest.raises(CampaignException) as exc_info:
        service.move_pipeline_stage(cc.id, PipelineStage.SELECTED, actor_id="user-1")

    assert exc_info.value.status_code == 409
    campaign_candidate_repo.rollback.assert_called_once()
    campaign_candidate_repo.commit.assert_not_called()


def test_move_pipeline_stage_rolls_back_and_raises_400_when_reason_required():
    cc = _campaign_candidate()
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    pipeline_transition_service = MagicMock()
    pipeline_transition_service.transition_stage.side_effect = PipelineTransitionReasonRequiredException(
        "INTERVIEW", "REJECTED",
    )
    service = make_service(
        campaign_candidate_repo=campaign_candidate_repo, pipeline_transition_service=pipeline_transition_service,
    )

    with pytest.raises(CampaignException) as exc_info:
        service.move_pipeline_stage(cc.id, PipelineStage.REJECTED, actor_id="user-1")

    assert exc_info.value.status_code == 400
    campaign_candidate_repo.rollback.assert_called_once()


def test_move_pipeline_stage_returns_updated_candidate_response():
    cc = _campaign_candidate(pipeline_stage=PipelineStage.SHORTLISTED)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    pipeline_transition_service = MagicMock()
    candidate_repo = MagicMock()
    candidate_repo.get_by_id.return_value = None
    resume_repo = MagicMock()
    resume_repo.get_by_id.return_value = None
    service = make_service(
        campaign_candidate_repo=campaign_candidate_repo, pipeline_transition_service=pipeline_transition_service,
        candidate_repo=candidate_repo, resume_repo=resume_repo,
    )

    result = service.move_pipeline_stage(cc.id, PipelineStage.SHORTLISTED, actor_id="user-1")

    assert result.campaign_candidate_id == cc.id
    assert result.pipeline_stage == PipelineStage.SHORTLISTED
