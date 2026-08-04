from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.services.campaign.semantic_scoring_service import (
    SemanticScoringService,
    MissingResumeEmbeddingError,
    MissingJDEmbeddingError,
    SCORE_CLAMPED_TO_ZERO_REASON,
)


def _make_campaign_candidate(score_breakdown=None):
    return SimpleNamespace(id=uuid4(), score_breakdown=score_breakdown, semantic_score=None, semantic_score_breakdown=None)


def _harness(
    campaign_candidate=None,
    resume_embedding=None,
    jd_embedding=None,
    similarity=0.8,
    resume_skills=None,
    jd_skills=None,
):
    campaign_candidate = campaign_candidate or _make_campaign_candidate()
    resume_embedding = resume_embedding or SimpleNamespace(id=uuid4(), embedding_model_version_id=uuid4())
    jd_embedding = jd_embedding or SimpleNamespace(id=uuid4(), embedding_model_version_id=uuid4())

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = campaign_candidate

    resume_repo = MagicMock()
    resume_repo.get_embedding.return_value = resume_embedding
    resume_repo.compute_semantic_similarity.return_value = similarity
    resume_repo.get_by_id.return_value = SimpleNamespace(parsed_json={"skills": resume_skills or []})

    jd_repo = MagicMock()
    jd_repo.get_embedding_by_jd_id.return_value = jd_embedding
    jd_repo.get_by_id.return_value = SimpleNamespace(
        extracted_json={"required_skills": jd_skills or [], "preferred_skills": []},
    )

    service = SemanticScoringService(resume_repo, jd_repo, campaign_candidate_repo)
    return service, campaign_candidate, campaign_candidate_repo, resume_repo, jd_repo


def test_raises_when_campaign_candidate_not_found():
    service, _, campaign_candidate_repo, _, _ = _harness()
    campaign_candidate_repo.get_by_id.return_value = None

    with pytest.raises(ValueError):
        service.calculate_and_store_semantic_score_breakdown(uuid4(), uuid4(), uuid4(), semantic_threshold=0.65)


def test_raises_missing_resume_embedding_error_when_resume_has_no_embedding():
    service, cc, _, resume_repo, _ = _harness()
    resume_repo.get_embedding.return_value = None

    with pytest.raises(MissingResumeEmbeddingError):
        service.calculate_and_store_semantic_score_breakdown(cc.id, uuid4(), uuid4(), semantic_threshold=0.65)


def test_raises_missing_jd_embedding_error_when_jd_has_no_embedding():
    service, cc, _, _, jd_repo = _harness()
    jd_repo.get_embedding_by_jd_id.return_value = None

    with pytest.raises(MissingJDEmbeddingError):
        service.calculate_and_store_semantic_score_breakdown(cc.id, uuid4(), uuid4(), semantic_threshold=0.65)


def test_semantic_score_above_threshold_passes():
    score_breakdown = {
        "mandatory_skills": [{"canonical_name": "Python", "match_type": "EXACT"}],
        "preferred_skills": [{"canonical_name": "Docker", "match_type": "MISSING"}],
    }
    cc = _make_campaign_candidate(score_breakdown=score_breakdown)
    service, cc, campaign_candidate_repo, _, _ = _harness(
        campaign_candidate=cc, similarity=0.9,
        resume_skills=["Python", "AWS"], jd_skills=["Python", "AWS"],
    )

    breakdown = service.calculate_and_store_semantic_score_breakdown(cc.id, uuid4(), uuid4(), semantic_threshold=0.65)

    assert breakdown["semantic_passed"] is True
    assert breakdown["overall_similarity"] == 0.9
    assert breakdown["semantic_threshold"] == 0.65
    assert breakdown["matching_skills"] == ["Python"]
    assert breakdown["missing_skills"] == ["Docker"]
    assert breakdown["matched_keywords"] == ["aws", "python"]
    assert cc.semantic_score == 0.9
    assert cc.semantic_score_breakdown == breakdown
    campaign_candidate_repo.update.assert_called_once_with(cc)


def test_semantic_score_below_threshold_fails():
    service, cc, _, _, _ = _harness(similarity=0.3)

    breakdown = service.calculate_and_store_semantic_score_breakdown(cc.id, uuid4(), uuid4(), semantic_threshold=0.65)

    assert breakdown["semantic_passed"] is False
    assert breakdown["overall_similarity"] == 0.3
    assert "falls short of" in breakdown["semantic_explanation"]


def test_uses_the_passed_in_semantic_threshold_not_a_global_default():
    """
    Story 541: the threshold comes from hiring_campaigns.semantic_threshold
    (passed in by the caller), never a global platform_config value or a
    hardcoded default - this service has no config_repository dependency
    at all anymore.
    """
    service, cc, _, _, _ = _harness(similarity=0.8)

    breakdown = service.calculate_and_store_semantic_score_breakdown(cc.id, uuid4(), uuid4(), semantic_threshold=0.42)

    assert breakdown["semantic_threshold"] == 0.42
    assert not hasattr(service, "config_repository")


def test_score_breakdown_generation_includes_all_required_fields():
    """Task 5's required output fields must all be present."""
    service, cc, _, _, _ = _harness(similarity=0.7)

    breakdown = service.calculate_and_store_semantic_score_breakdown(cc.id, uuid4(), uuid4(), semantic_threshold=0.65)

    for key in (
        "semantic_score", "semantic_passed", "overall_similarity", "semantic_threshold",
        "matching_skills", "missing_skills", "matched_keywords", "semantic_explanation",
    ):
        assert key in breakdown


# ----------------------------------------------------------------------
# Story 538 - entire similarity computation runs in Postgres via
# ResumeRepository.compute_semantic_similarity (never a manual Python
# vector calculation, never get_cosine_similarity's "fetch the vector into
# memory first" approach), filtered by resume_id/jd_id directly.
# ----------------------------------------------------------------------

def test_computes_similarity_via_pgvector_repository_method_with_resume_and_jd_ids():
    service, cc, _, resume_repo, _ = _harness(similarity=0.75)
    resume_id, jd_id = uuid4(), uuid4()

    service.calculate_and_store_semantic_score_breakdown(cc.id, jd_id, resume_id, semantic_threshold=0.65)

    resume_repo.compute_semantic_similarity.assert_called_once_with(resume_id, jd_id)


def test_raises_missing_resume_embedding_error_when_similarity_is_none():
    """
    compute_semantic_similarity returns None when either row is gone
    (concurrent deletion) or embedding_model_version_id no longer match -
    a race condition, not a routine skip (that's handled earlier, at the
    task layer, before this service is ever called).
    """
    service, cc, _, resume_repo, _ = _harness()
    resume_repo.compute_semantic_similarity.return_value = None

    with pytest.raises(MissingResumeEmbeddingError):
        service.calculate_and_store_semantic_score_breakdown(cc.id, uuid4(), uuid4(), semantic_threshold=0.65)


def test_breakdown_stores_computation_duration_ms():
    service, cc, _, _, _ = _harness(similarity=0.8)

    breakdown = service.calculate_and_store_semantic_score_breakdown(cc.id, uuid4(), uuid4(), semantic_threshold=0.65)

    assert isinstance(breakdown["computation_duration_ms"], int)
    assert breakdown["computation_duration_ms"] >= 0


# ----------------------------------------------------------------------
# Task 539 - jd_embedding_id recorded under semantic_check, and
# semantic_score/semantic_score_computed_at/updated_at all set together.
# ----------------------------------------------------------------------

def test_semantic_check_records_jd_and_resume_embedding_ids():
    resume_embedding = SimpleNamespace(id=uuid4(), embedding_model_version_id=uuid4())
    jd_embedding = SimpleNamespace(id=uuid4(), embedding_model_version_id=uuid4())
    service, cc, _, _, _ = _harness(resume_embedding=resume_embedding, jd_embedding=jd_embedding)

    breakdown = service.calculate_and_store_semantic_score_breakdown(cc.id, uuid4(), uuid4(), semantic_threshold=0.65)

    assert breakdown["semantic_check"]["jd_embedding_id"] == str(jd_embedding.id)
    assert breakdown["semantic_check"]["resume_embedding_id"] == str(resume_embedding.id)


def test_semantic_score_computed_at_and_updated_at_set_atomically():
    service, cc, campaign_candidate_repo, _, _ = _harness(similarity=0.8)
    assert cc.semantic_score_breakdown is None

    service.calculate_and_store_semantic_score_breakdown(cc.id, uuid4(), uuid4(), semantic_threshold=0.65)

    assert cc.semantic_score_computed_at is not None
    assert cc.updated_at == cc.semantic_score_computed_at
    campaign_candidate_repo.update.assert_called_once_with(cc)


# ----------------------------------------------------------------------
# Task 540 - all scores >= 0.0 are valid as-is; only a genuinely negative
# similarity is clamped, and only ever up to 0.0000; threshold comparison
# uses the clamped value; four-decimal rounding (NUMERIC(5,4)).
# ----------------------------------------------------------------------

def test_very_low_but_non_negative_score_is_not_clamped_and_can_still_fail():
    service, cc, _, _, _ = _harness(similarity=0.0001)

    breakdown = service.calculate_and_store_semantic_score_breakdown(cc.id, uuid4(), uuid4(), semantic_threshold=0.65)

    assert breakdown["semantic_score"] == 0.0001
    assert breakdown["score_clamped_to_zero"] is False
    assert breakdown["score_clamp_reason"] is None
    assert breakdown["semantic_passed"] is False


def test_negative_similarity_is_clamped_to_zero_and_flagged():
    service, cc, _, _, _ = _harness(similarity=-0.0003)

    breakdown = service.calculate_and_store_semantic_score_breakdown(cc.id, uuid4(), uuid4(), semantic_threshold=0.65)

    assert breakdown["semantic_score"] == 0.0
    assert breakdown["score_clamped_to_zero"] is True
    assert breakdown["score_clamp_reason"] == SCORE_CLAMPED_TO_ZERO_REASON
    assert cc.semantic_score == 0.0


def test_threshold_comparison_uses_the_clamped_score_not_the_raw_negative_one():
    """A threshold of 0.0 (or below) must still be met by a clamped 0.0 score."""
    service, cc, _, _, _ = _harness(similarity=-0.02)

    breakdown = service.calculate_and_store_semantic_score_breakdown(cc.id, uuid4(), uuid4(), semantic_threshold=0.0)

    assert breakdown["semantic_passed"] is True


def test_semantic_score_rounded_to_four_decimal_places():
    service, cc, _, _, _ = _harness(similarity=0.123456789)

    breakdown = service.calculate_and_store_semantic_score_breakdown(cc.id, uuid4(), uuid4(), semantic_threshold=0.65)

    assert breakdown["semantic_score"] == 0.1235
