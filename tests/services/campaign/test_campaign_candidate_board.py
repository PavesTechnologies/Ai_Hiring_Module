from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.models.pipeline import PipelineStage
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
    PipelineStage.HOLD, PipelineStage.HM_REVIEW, PipelineStage.INTERVIEW,
    PipelineStage.SELECTED, PipelineStage.REJECTED,
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


def test_get_campaign_board_includes_all_eight_columns_even_when_empty():
    service = make_service()
    with patch.object(service, "get_campaign_candidates", return_value=[]):
        result = service.get_campaign_board(uuid4())

    assert [col.stage for col in result.columns] == _BOARD_STAGES
    assert all(col.count == 0 and col.candidates == [] for col in result.columns)


def test_get_campaign_board_includes_hm_review_as_its_own_column():
    service = make_service()
    items = [
        _candidate_response(PipelineStage.HM_REVIEW),
        _candidate_response(PipelineStage.HM_REVIEW),
        _candidate_response(PipelineStage.SCREENING),
    ]
    with patch.object(service, "get_campaign_candidates", return_value=items):
        result = service.get_campaign_board(uuid4())

    by_stage = {col.stage: col for col in result.columns}
    assert by_stage[PipelineStage.HM_REVIEW].count == 2
    assert result.other_count == 0


def test_get_campaign_board_counts_fraud_review_as_other():
    """FRAUD_REVIEW isn't a board column - candidates in it must still be accounted for, not silently dropped."""
    service = make_service()
    items = [
        _candidate_response(PipelineStage.FRAUD_REVIEW),
        _candidate_response(PipelineStage.SCREENING),
    ]
    with patch.object(service, "get_campaign_candidates", return_value=items):
        result = service.get_campaign_board(uuid4())

    assert result.other_count == 1
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

# ----------------------------------------------------------------------
# M12 follow-up - CANDIDATE_SELECTED email is no longer queued
# automatically from any stage-move path, including move_pipeline_stage.
# Sending it is now a manual action - see
# CampaignCandidateService.send_selection_email and
# test_campaign_candidate_send_selection_email.py.
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# Epic 5 follow-up - manual re-score trigger's enqueue half (the cascade-
# cancel half lives inside transition_stage() itself, tested in
# test_pipeline_transition_service.py - transition_stage is mocked out
# entirely in this file's tests, matching its existing convention).
# ----------------------------------------------------------------------

