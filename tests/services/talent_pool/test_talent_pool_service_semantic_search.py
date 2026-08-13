from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.exception_handler.exceptions import BadRequestError, UnprocessableError
from app.schemas.talent_pool.talent_pool_schema import TalentPoolSemanticSearchFilters
from app.services.talent_pool.talent_pool_service import TALENT_POOL_MAX_PAGE_SIZE, TalentPoolService

"""
M14 - POST /talent-pool/semantic-search orchestration.
semantic_search_talent_pool (the SQL/ranking merits) is exercised on its own
in test_resume_repository_talent_pool_semantic_search.py; these tests only
verify TalentPoolService.semantic_search_candidates' own orchestration:
query validation, exactly one embedding generated per request, the active
embedding model version being looked up, page-size capping, and the batched
candidate/enrichment lookups feeding the response - mirrors
test_talent_pool_service_search.py's established convention.
"""


def _make_candidate(candidate_id=None):
    return SimpleNamespace(
        id=candidate_id or uuid4(),
        full_name_encrypted=b"encrypted-name",
        email_encrypted=b"encrypted-email",
        encryption_key_id=uuid4(),
        jurisdiction="GLOBAL",
        created_at=datetime.now(timezone.utc),
    )


def _make_resume(candidate_id, resume_id=None, version_number=1, parsed_json=None):
    return SimpleNamespace(
        id=resume_id or uuid4(),
        candidate_id=candidate_id,
        version_number=version_number,
        parsed_json=parsed_json,
        created_at=datetime.now(timezone.utc),
    )


def make_service(resume_repo=None, candidate_repo=None, campaign_candidate_repo=None, embedding_service=None):
    resume_repo = resume_repo or MagicMock()
    if not isinstance(resume_repo.semantic_search_talent_pool.return_value, tuple):
        resume_repo.semantic_search_talent_pool.return_value = ([], 0)
    if not isinstance(resume_repo.get_active_embedding_model_version.return_value, SimpleNamespace):
        resume_repo.get_active_embedding_model_version.return_value = SimpleNamespace(id=uuid4())
    if not isinstance(resume_repo.get_canonical_skills_by_resume_ids.return_value, dict):
        resume_repo.get_canonical_skills_by_resume_ids.return_value = {}

    candidate_repo = candidate_repo or MagicMock()
    if not isinstance(candidate_repo.get_by_ids.return_value, list):
        candidate_repo.get_by_ids.return_value = []

    campaign_candidate_repo = campaign_candidate_repo or MagicMock()
    if not isinstance(campaign_candidate_repo.get_best_composite_scores_by_candidate_ids.return_value, dict):
        campaign_candidate_repo.get_best_composite_scores_by_candidate_ids.return_value = {}

    embedding_service = embedding_service or MagicMock()
    if not isinstance(embedding_service.generate_embedding.return_value, list):
        embedding_service.generate_embedding.return_value = [0.1] * 384

    encryption_service = MagicMock()
    encryption_service.decrypt.side_effect = (
        lambda ciphertext, key_id: "Jane Doe" if ciphertext == b"encrypted-name" else "jane@example.com"
    )

    return TalentPoolService(
        candidate_repo=candidate_repo,
        resume_repo=resume_repo,
        campaign_repo=MagicMock(),
        campaign_candidate_repo=campaign_candidate_repo,
        consent_repo=MagicMock(),
        encryption_service=encryption_service,
        audit_service=MagicMock(),
        celery_task_log_service=MagicMock(),
        resume_selection_service=MagicMock(),
        skill_repo=MagicMock(),
        config_repo=None,
        embedding_service=embedding_service,
    ), resume_repo, candidate_repo, embedding_service


def test_empty_query_raises_bad_request():
    service, _, _, _ = make_service()

    with pytest.raises(BadRequestError):
        service.semantic_search_candidates(query="", page=1, size=6)


def test_whitespace_only_query_raises_bad_request():
    service, _, _, _ = make_service()

    with pytest.raises(BadRequestError):
        service.semantic_search_candidates(query="   \t\n  ", page=1, size=6)


def test_query_is_trimmed_before_being_embedded():
    service, _, _, embedding_service = make_service()

    service.semantic_search_candidates(query="  senior python engineer  ", page=1, size=6)

    embedding_service.generate_embedding.assert_called_once_with("senior python engineer")


def test_no_embedding_service_configured_raises_unprocessable():
    resume_repo = MagicMock()
    service, _, _, _ = make_service(resume_repo=resume_repo)
    service.embedding_service = None

    with pytest.raises(UnprocessableError):
        service.semantic_search_candidates(query="senior python engineer", page=1, size=6)


def test_embedding_generation_failure_is_wrapped_as_unprocessable():
    embedding_service = MagicMock()
    embedding_service.generate_embedding.side_effect = RuntimeError("model load failed")
    service, _, _, _ = make_service(embedding_service=embedding_service)

    with pytest.raises(UnprocessableError):
        service.semantic_search_candidates(query="senior python engineer", page=1, size=6)


def test_exactly_one_embedding_is_generated_per_search_request():
    service, _, _, embedding_service = make_service()

    service.semantic_search_candidates(query="senior python engineer", page=1, size=6)

    embedding_service.generate_embedding.assert_called_once()


def test_query_embedding_and_active_model_version_are_passed_to_the_repository():
    resume_repo = MagicMock()
    resume_repo.semantic_search_talent_pool.return_value = ([], 0)
    resume_repo.get_canonical_skills_by_resume_ids.return_value = {}
    model_version_id = uuid4()
    resume_repo.get_active_embedding_model_version.return_value = SimpleNamespace(id=model_version_id)
    embedding_vector = [0.42] * 384
    embedding_service = MagicMock()
    embedding_service.generate_embedding.return_value = embedding_vector
    service, resume_repo, _, _ = make_service(resume_repo=resume_repo, embedding_service=embedding_service)

    service.semantic_search_candidates(query="senior python engineer", page=1, size=6)

    kwargs = resume_repo.semantic_search_talent_pool.call_args.kwargs
    assert kwargs["query_embedding"] == embedding_vector
    assert kwargs["embedding_model_version_id"] == model_version_id


def test_structured_filters_are_forwarded_to_the_repository():
    resume_repo = MagicMock()
    resume_repo.semantic_search_talent_pool.return_value = ([], 0)
    resume_repo.get_canonical_skills_by_resume_ids.return_value = {}
    service, resume_repo, _, _ = make_service(resume_repo=resume_repo)
    filters = TalentPoolSemanticSearchFilters(
        locations=["Hyderabad", "Chennai"],
        designations=["Backend Engineer"],
        experience_min=5,
        experience_max=10,
        score_min=60,
        score_max=100,
    )

    service.semantic_search_candidates(query="senior python engineer", filters=filters, page=1, size=6)

    kwargs = resume_repo.semantic_search_talent_pool.call_args.kwargs
    assert kwargs["location_terms"] == ["Hyderabad", "Chennai"]
    assert kwargs["designation_terms"] == ["Backend Engineer"]
    assert kwargs["experience_min"] == 5
    assert kwargs["experience_max"] == 10
    assert kwargs["score_min"] == 60
    assert kwargs["score_max"] == 100


def test_response_size_is_capped_end_to_end_when_a_larger_size_is_requested():
    service, resume_repo, _, _ = make_service()

    data = service.semantic_search_candidates(query="senior python engineer", page=1, size=100)

    assert data.size == TALENT_POOL_MAX_PAGE_SIZE
    assert resume_repo.semantic_search_talent_pool.call_args.kwargs["size"] == TALENT_POOL_MAX_PAGE_SIZE


def test_no_result_case_returns_empty_response_not_an_error():
    service, _, _, _ = make_service()

    data = service.semantic_search_candidates(query="a very unusual candidate profile", page=1, size=6)

    assert data.items == []
    assert data.total == 0
    assert data.page == 1
    assert data.size == 6


def test_response_includes_semantic_similarity_score_per_item():
    candidate_id = uuid4()
    candidate = _make_candidate(candidate_id)
    resume = _make_resume(candidate_id, parsed_json={"summary": "Backend engineer"})

    resume_repo = MagicMock()
    resume_repo.semantic_search_talent_pool.return_value = ([(resume, 0.8734)], 1)
    resume_repo.get_active_embedding_model_version.return_value = SimpleNamespace(id=uuid4())
    resume_repo.get_canonical_skills_by_resume_ids.return_value = {resume.id: ["Python", "AWS"]}

    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = [candidate]

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_best_composite_scores_by_candidate_ids.return_value = {candidate_id: 88.5}

    service, _, _, _ = make_service(
        resume_repo=resume_repo, candidate_repo=candidate_repo, campaign_candidate_repo=campaign_candidate_repo,
    )

    data = service.semantic_search_candidates(query="senior backend engineer", page=1, size=6)

    assert len(data.items) == 1
    item = data.items[0]
    assert item.semantic_similarity_score == 0.8734
    assert item.best_composite_score == 88.5
    assert item.skills == ["Python", "AWS"]
    # best_composite_score must never be conflated with the semantic score.
    assert item.semantic_similarity_score != item.best_composite_score
