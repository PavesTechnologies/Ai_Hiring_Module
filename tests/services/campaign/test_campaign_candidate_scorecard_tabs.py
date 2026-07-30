from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.exceptions.campaign_exceptions import CampaignException
from app.models.pipeline import AIRecommendation, PipelineStage, RejectionLayer
from app.schemas.campaign.campaign_candidate_schema import (
    CandidateDeterministicResponse,
    CandidateSummaryResponse,
)
from app.services.campaign.campaign_candidate_service import CampaignCandidateService

"""
Candidate Scorecard tab endpoints - get_candidate_summary /
get_candidate_deterministic. Both reuse the exact same shared mapper
helpers the full aggregate scorecard endpoint uses
(_to_campaign_candidate_response / _build_rejection_banner /
_build_deterministic_score_breakdown) - these tests verify each tab
response only exposes what its own contract allows, nothing is
recomputed independently, and the full aggregate endpoint stays
unaffected.
"""


def _make_campaign_candidate(
    pipeline_stage=PipelineStage.SCREENING,
    score_breakdown=None,
    deterministic_score=78.5,
    ai_recommendation=None,
    ai_strengths=None,
    ai_weaknesses=None,
):
    return SimpleNamespace(
        id=uuid4(),
        campaign_id=uuid4(),
        candidate_id=uuid4(),
        resume_id=uuid4(),
        pipeline_stage=pipeline_stage,
        score_breakdown=score_breakdown,
        hr_override=False,
        deterministic_score=deterministic_score,
        ai_ats_score=None,
        semantic_score=None,
        composite_score=None,
        screened_at=None,
        ai_recommendation=ai_recommendation,
        ai_strengths=ai_strengths,
        ai_weaknesses=ai_weaknesses,
        created_at=datetime.now(timezone.utc),
    )


def make_service(campaign_candidate_repo=None, candidate_rejection_repo=None, config_repo=None):
    return CampaignCandidateService(
        campaign_repo=MagicMock(),
        campaign_candidate_repo=campaign_candidate_repo or MagicMock(),
        audit_service=MagicMock(),
        candidate_rejection_repo=candidate_rejection_repo,
        config_repo=config_repo,
    )


# ----------------------------------------------------------------------
# get_candidate_summary
# ----------------------------------------------------------------------

def test_summary_not_found_when_campaign_candidate_missing():
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = None
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    with pytest.raises(CampaignException) as exc_info:
        service.get_candidate_summary(uuid4())

    assert exc_info.value.status_code == 404


def test_summary_returns_only_summary_fields():
    candidate = _make_campaign_candidate()
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate

    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    result = service.get_candidate_summary(candidate.id)

    assert isinstance(result, CandidateSummaryResponse)
    assert result.campaign_candidate_id == candidate.id
    assert result.campaign_id == candidate.campaign_id
    assert result.candidate_id == candidate.candidate_id
    assert result.pipeline_stage == PipelineStage.SCREENING
    assert result.deterministic_score == 78.5
    assert result.ai_summary is None

    # Never includes deterministic breakdown, resume, or rejection/override
    # banner fields - those belong to other (existing or future) tabs.
    assert not hasattr(result, "score_breakdown")
    assert not hasattr(result, "deterministic_score_breakdown")
    assert not hasattr(result, "has_rejection")
    assert not hasattr(result, "rejection_reason")
    assert not hasattr(result, "is_overridden")
    assert not hasattr(result, "resume_id")


def test_summary_ai_summary_null_when_no_ai_evaluation_columns_set():
    candidate = _make_campaign_candidate(ai_recommendation=None, ai_strengths=None, ai_weaknesses=None)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    result = service.get_candidate_summary(candidate.id)

    assert result.ai_summary is None


def test_summary_ai_summary_populated_when_ai_evaluation_columns_are_set():
    candidate = _make_campaign_candidate(
        ai_recommendation=AIRecommendation.SHORTLIST,
        ai_strengths={"communication": "excellent"},
        ai_weaknesses={"experience": "limited in cloud infra"},
    )
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    result = service.get_candidate_summary(candidate.id)

    assert result.ai_summary is not None
    assert result.ai_summary.recommendation == AIRecommendation.SHORTLIST.value
    assert result.ai_summary.strengths == {"communication": "excellent"}
    assert result.ai_summary.weaknesses == {"experience": "limited in cloud infra"}


# ----------------------------------------------------------------------
# get_candidate_deterministic
# ----------------------------------------------------------------------

def test_deterministic_not_found_when_campaign_candidate_missing():
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = None
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    with pytest.raises(CampaignException) as exc_info:
        service.get_candidate_deterministic(uuid4())

    assert exc_info.value.status_code == 404


def test_deterministic_returns_null_breakdown_when_scoring_not_yet_run():
    candidate = _make_campaign_candidate(score_breakdown=None)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    result = service.get_candidate_deterministic(candidate.id)

    assert isinstance(result, CandidateDeterministicResponse)
    assert result.campaign_candidate_id == candidate.id
    assert result.deterministic_score == 78.5
    assert result.deterministic_score_breakdown is None


def test_deterministic_maps_full_breakdown_and_excludes_summary_fields():
    breakdown = {
        "deterministic_score": 78.5,
        "deterministic_passed": True,
        "deterministic_threshold": 70.0,
        "mandatory_coverage_pct": 100.0,
        "mandatory_skills": [],
        "preferred_skills": [],
    }
    candidate = _make_campaign_candidate(score_breakdown=breakdown)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate

    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    result = service.get_candidate_deterministic(candidate.id)

    assert result.deterministic_score_breakdown is not None
    assert result.deterministic_score_breakdown.summary.overall_score == 78.5
    assert result.deterministic_score_breakdown.summary.status == "PASSED"

    # Never includes candidate/summary-specific fields.
    assert not hasattr(result, "candidate_name")
    assert not hasattr(result, "pipeline_stage")
    assert not hasattr(result, "ai_summary")


def test_deterministic_reuses_rejection_banner_for_failure_reason():
    """
    Confirms get_candidate_deterministic threads the SAME rejection_reason
    the full scorecard endpoint uses into deterministic_score_breakdown -
    not a second/independent lookup.
    """
    breakdown = {
        "deterministic_score": 40.0, "deterministic_passed": False, "deterministic_threshold": 70.0,
        "mandatory_coverage_pct": 50.0, "mandatory_skills": [], "preferred_skills": [],
    }
    candidate = _make_campaign_candidate(pipeline_stage=PipelineStage.REJECTED, score_breakdown=breakdown)
    candidate.hr_override = False
    rejection = SimpleNamespace(
        id=uuid4(), rejection_layer=RejectionLayer.DETERMINISTIC,
        rejection_reason="Missing required skills: Python.", rejected_at=datetime.now(timezone.utc),
    )
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate
    candidate_rejection_repo = MagicMock()
    candidate_rejection_repo.get_by_campaign_candidate_id.return_value = [rejection]

    service = make_service(campaign_candidate_repo=campaign_candidate_repo, candidate_rejection_repo=candidate_rejection_repo)

    result = service.get_candidate_deterministic(candidate.id)

    assert result.deterministic_score_breakdown.summary.failure_reason == "Missing required skills: Python."
    assert result.deterministic_score_breakdown.summary.failure_reasons == ["Missing required skills: Python."]


def test_full_scorecard_endpoint_still_returns_the_complete_aggregate_response():
    """
    Regression: get_campaign_candidate_scorecard (the pre-existing,
    "must keep working without changes" endpoint) is untouched by the
    new tab methods - still returns everything in one object.
    """
    breakdown = {
        "deterministic_score": 78.5, "deterministic_passed": True, "deterministic_threshold": 70.0,
        "mandatory_coverage_pct": 100.0, "mandatory_skills": [], "preferred_skills": [],
    }
    candidate = _make_campaign_candidate(score_breakdown=breakdown)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    result = service.get_campaign_candidate_scorecard(candidate.id)

    assert result.deterministic_score_breakdown is not None
    assert hasattr(result, "has_rejection")
    assert hasattr(result, "score_breakdown")
    assert hasattr(result, "candidate_name")
