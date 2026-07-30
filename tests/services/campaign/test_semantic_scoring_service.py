from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.services.campaign.semantic_scoring_service import (
    SemanticScoringService,
    MissingResumeEmbeddingError,
    MissingJDEmbeddingError,
    _DEFAULT_SEMANTIC_PASS_THRESHOLD,
)


def _make_campaign_candidate(score_breakdown=None):
    return SimpleNamespace(id=uuid4(), score_breakdown=score_breakdown, semantic_score=None, semantic_score_breakdown=None)


def _harness(
    campaign_candidate=None,
    resume_embedding=SimpleNamespace(id=uuid4(), embedding_model_version_id=uuid4()),
    jd_embedding=SimpleNamespace(embedding=[0.1] * 384, embedding_model_version_id=uuid4()),
    similarity=0.8,
    threshold_config=None,
    resume_skills=None,
    jd_skills=None,
):
    campaign_candidate = campaign_candidate or _make_campaign_candidate()

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = campaign_candidate

    resume_repo = MagicMock()
    resume_repo.get_embedding.return_value = resume_embedding
    resume_repo.get_cosine_similarity.return_value = similarity
    resume_repo.get_by_id.return_value = SimpleNamespace(parsed_json={"skills": resume_skills or []})

    jd_repo = MagicMock()
    jd_repo.get_embedding_by_jd_id.return_value = jd_embedding
    jd_repo.get_by_id.return_value = SimpleNamespace(
        extracted_json={"required_skills": jd_skills or [], "preferred_skills": []},
    )

    config_repo = MagicMock()
    config_repo.get_configs_by_keys.return_value = (
        {} if threshold_config is None else {"SEMANTIC_PASS_THRESHOLD": threshold_config}
    )

    service = SemanticScoringService(resume_repo, jd_repo, config_repo, campaign_candidate_repo)
    return service, campaign_candidate, campaign_candidate_repo, resume_repo, jd_repo


def test_raises_when_campaign_candidate_not_found():
    service, _, campaign_candidate_repo, _, _ = _harness()
    campaign_candidate_repo.get_by_id.return_value = None

    with pytest.raises(ValueError):
        service.calculate_and_store_semantic_score_breakdown(uuid4(), uuid4(), uuid4())


def test_raises_missing_resume_embedding_error_when_resume_has_no_embedding():
    service, cc, _, resume_repo, _ = _harness()
    resume_repo.get_embedding.return_value = None

    with pytest.raises(MissingResumeEmbeddingError):
        service.calculate_and_store_semantic_score_breakdown(cc.id, uuid4(), uuid4())


def test_raises_missing_jd_embedding_error_when_jd_has_no_embedding():
    service, cc, _, _, jd_repo = _harness()
    jd_repo.get_embedding_by_jd_id.return_value = None

    with pytest.raises(MissingJDEmbeddingError):
        service.calculate_and_store_semantic_score_breakdown(cc.id, uuid4(), uuid4())


def test_semantic_score_above_threshold_passes():
    score_breakdown = {
        "mandatory_skills": [{"canonical_name": "Python", "match_type": "EXACT"}],
        "preferred_skills": [{"canonical_name": "Docker", "match_type": "MISSING"}],
    }
    cc = _make_campaign_candidate(score_breakdown=score_breakdown)
    service, cc, campaign_candidate_repo, _, _ = _harness(
        campaign_candidate=cc, similarity=0.9, threshold_config="0.65",
        resume_skills=["Python", "AWS"], jd_skills=["Python", "AWS"],
    )

    breakdown = service.calculate_and_store_semantic_score_breakdown(cc.id, uuid4(), uuid4())

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
    service, cc, _, _, _ = _harness(similarity=0.3, threshold_config="0.65")

    breakdown = service.calculate_and_store_semantic_score_breakdown(cc.id, uuid4(), uuid4())

    assert breakdown["semantic_passed"] is False
    assert breakdown["overall_similarity"] == 0.3
    assert "falls short of" in breakdown["semantic_explanation"]


def test_uses_default_threshold_when_config_missing():
    service, cc, _, _, _ = _harness(similarity=0.8, threshold_config=None)

    breakdown = service.calculate_and_store_semantic_score_breakdown(cc.id, uuid4(), uuid4())

    assert breakdown["semantic_threshold"] == _DEFAULT_SEMANTIC_PASS_THRESHOLD


def test_score_breakdown_generation_includes_all_required_fields():
    """Task 5's required output fields must all be present."""
    service, cc, _, _, _ = _harness(similarity=0.7, threshold_config="0.65")

    breakdown = service.calculate_and_store_semantic_score_breakdown(cc.id, uuid4(), uuid4())

    for key in (
        "semantic_score", "semantic_passed", "overall_similarity", "semantic_threshold",
        "matching_skills", "missing_skills", "matched_keywords", "semantic_explanation",
    ):
        assert key in breakdown
