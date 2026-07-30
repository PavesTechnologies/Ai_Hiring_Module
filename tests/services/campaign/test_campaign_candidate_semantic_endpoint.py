from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.exceptions.campaign_exceptions import CampaignException
from app.models.pipeline import PipelineStage
from app.schemas.campaign.campaign_candidate_schema import CandidateSemanticResponse
from app.services.campaign.campaign_candidate_service import CampaignCandidateService

"""
get_candidate_semantic (M08-E02 Phase 2) - mirrors get_candidate_deterministic
exactly: a pure read/transform of campaign_candidates.semantic_score/
semantic_score_breakdown, never a recalculation. Also covers regression
verification for the three pre-existing endpoints (aggregate scorecard,
summary tab, deterministic tab) to confirm none of them are affected by
this addition.
"""


def _make_campaign_candidate(
    pipeline_stage=PipelineStage.SCREENING,
    score_breakdown=None,
    semantic_score_breakdown=None,
    deterministic_score=78.5,
    semantic_score=None,
):
    return SimpleNamespace(
        id=uuid4(),
        campaign_id=uuid4(),
        candidate_id=uuid4(),
        resume_id=uuid4(),
        pipeline_stage=pipeline_stage,
        score_breakdown=score_breakdown,
        semantic_score_breakdown=semantic_score_breakdown,
        hr_override=False,
        deterministic_score=deterministic_score,
        ai_ats_score=None,
        semantic_score=semantic_score,
        composite_score=None,
        screened_at=None,
        ai_recommendation=None,
        ai_strengths=None,
        ai_weaknesses=None,
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
# get_candidate_semantic
# ----------------------------------------------------------------------

def test_semantic_not_found_when_campaign_candidate_missing():
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = None
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    with pytest.raises(CampaignException) as exc_info:
        service.get_candidate_semantic(uuid4())

    assert exc_info.value.status_code == 404


def test_semantic_returns_null_breakdown_when_scoring_not_yet_run():
    candidate = _make_campaign_candidate(semantic_score_breakdown=None, semantic_score=None)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    result = service.get_candidate_semantic(candidate.id)

    assert isinstance(result, CandidateSemanticResponse)
    assert result.campaign_candidate_id == candidate.id
    assert result.semantic_score is None
    assert result.semantic_score_breakdown is None


def test_semantic_maps_full_breakdown_and_excludes_other_tab_fields():
    breakdown = {
        "semantic_score": 0.812345,
        "overall_similarity": 0.812345,
        "semantic_passed": True,
        "semantic_threshold": 0.65,
        "matching_skills": ["Python", "SQL"],
        "missing_skills": ["Kubernetes"],
        "matched_keywords": ["python", "sql"],
        "semantic_explanation": "Resume-to-job semantic similarity is 81.2%, which meets the threshold.",
        "computed_at": "2026-07-29T10:15:00+00:00",
    }
    candidate = _make_campaign_candidate(semantic_score_breakdown=breakdown, semantic_score=0.812345)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    result = service.get_candidate_semantic(candidate.id)

    assert result.semantic_score == 0.812345
    assert result.semantic_score_breakdown is not None
    assert result.semantic_score_breakdown.summary.overall_score == 0.812345
    assert result.semantic_score_breakdown.summary.status == "PASSED"
    assert result.semantic_score_breakdown.summary.matching_skills_count == 2
    assert result.semantic_score_breakdown.summary.missing_skills_count == 1
    assert result.semantic_score_breakdown.overall_similarity == 0.812345
    assert result.semantic_score_breakdown.matching_skills == ["Python", "SQL"]
    assert result.semantic_score_breakdown.missing_skills == ["Kubernetes"]
    assert result.semantic_score_breakdown.matched_keywords == ["python", "sql"]
    assert result.semantic_score_breakdown.semantic_explanation == breakdown["semantic_explanation"]

    # Never includes candidate/summary/deterministic-specific fields.
    assert not hasattr(result, "candidate_name")
    assert not hasattr(result, "pipeline_stage")
    assert not hasattr(result, "ai_summary")
    assert not hasattr(result, "deterministic_score")
    assert not hasattr(result, "score_breakdown")
    assert not hasattr(result, "resume_id")


def test_semantic_shows_failed_status_when_below_threshold():
    breakdown = {
        "semantic_score": 0.3, "overall_similarity": 0.3, "semantic_passed": False,
        "semantic_threshold": 0.65, "matching_skills": [], "missing_skills": ["Python"],
        "matched_keywords": [], "semantic_explanation": "Resume-to-job semantic similarity is 30.0%, which falls short.",
    }
    candidate = _make_campaign_candidate(semantic_score_breakdown=breakdown, semantic_score=0.3)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    result = service.get_candidate_semantic(candidate.id)

    assert result.semantic_score_breakdown.summary.status == "FAILED"
    assert result.semantic_score_breakdown.summary.failure_reason == breakdown["semantic_explanation"]
    assert result.semantic_score_breakdown.semantic_passed is False


# ----------------------------------------------------------------------
# Regression: pre-existing endpoints must be unaffected
# ----------------------------------------------------------------------

def test_regression_full_scorecard_endpoint_unaffected_by_semantic_addition():
    breakdown = {
        "deterministic_score": 78.5, "deterministic_passed": True, "deterministic_threshold": 70.0,
        "mandatory_coverage_pct": 100.0, "mandatory_skills": [], "preferred_skills": [],
    }
    candidate = _make_campaign_candidate(score_breakdown=breakdown, semantic_score=0.7)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    result = service.get_campaign_candidate_scorecard(candidate.id)

    assert result.deterministic_score_breakdown is not None
    assert result.semantic_score == 0.7
    assert hasattr(result, "has_rejection")
    assert hasattr(result, "score_breakdown")
    assert hasattr(result, "candidate_name")
    # The aggregate response has no semantic_score_breakdown field -
    # that's the semantic tab's own concern.
    assert not hasattr(result, "semantic_score_breakdown")


def test_regression_summary_endpoint_unaffected_by_semantic_addition():
    candidate = _make_campaign_candidate(semantic_score=0.7)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    result = service.get_candidate_summary(candidate.id)

    assert result.semantic_score == 0.7
    assert result.deterministic_score == 78.5
    assert not hasattr(result, "semantic_score_breakdown")
    assert not hasattr(result, "deterministic_score_breakdown")


def test_regression_deterministic_endpoint_unaffected_by_semantic_addition():
    breakdown = {
        "deterministic_score": 78.5, "deterministic_passed": True, "deterministic_threshold": 70.0,
        "mandatory_coverage_pct": 100.0, "mandatory_skills": [], "preferred_skills": [],
    }
    candidate = _make_campaign_candidate(score_breakdown=breakdown, semantic_score=0.7)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    result = service.get_candidate_deterministic(candidate.id)

    assert result.deterministic_score_breakdown is not None
    assert result.deterministic_score_breakdown.summary.overall_score == 78.5
    assert not hasattr(result, "semantic_score")
    assert not hasattr(result, "semantic_score_breakdown")
