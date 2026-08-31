"""
POST /talent-pool/semantic-search - M14 Talent Pool Semantic Search route
contract. Same MagicMock-based approach as test_talent_pool_search_routes.py
(no TestClient/live-Postgres infra in this project): inspects the actual
FastAPI route/dependency graph built at import time, plus a direct call
through service.semantic_search_candidates with a mocked repository layer.
"""
import inspect
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from app.api.routes.talent_pool_routes import router
from app.models.identity import UserRole
from app.schemas.response import APIResponse
from app.schemas.talent_pool.talent_pool_schema import TalentPoolSemanticSearchRequest
from app.services.talent_pool.talent_pool_service import TALENT_POOL_MAX_PAGE_SIZE, TalentPoolService

_SEMANTIC_SEARCH_PATH = "/talent-pool/semantic-search"
_NORMAL_SEARCH_PATH = "/talent-pool/candidates"


def _get_route(path: str, method: str):
    for route in router.routes:
        if route.path == path and method in route.methods:
            return route
    raise AssertionError(f"No {method} route registered for {path}")


def _allowed_roles(route) -> frozenset:
    for dependency in route.dependant.dependencies:
        call = dependency.call
        if call.__name__ == "_check" and "allowed" in call.__code__.co_freevars:
            index = call.__code__.co_freevars.index("allowed")
            return call.__closure__[index].cell_contents
    raise AssertionError(f"No require_roles(...) dependency found on {route.path}")


def test_semantic_search_route_is_registered_at_the_preferred_path():
    route = _get_route(_SEMANTIC_SEARCH_PATH, "POST")
    assert route.path == _SEMANTIC_SEARCH_PATH


def test_semantic_search_route_allows_hr_admin_recruiter_and_hiring_manager():
    allowed = _allowed_roles(_get_route(_SEMANTIC_SEARCH_PATH, "POST"))
    assert allowed == frozenset({
        UserRole.HR_ADMIN.value, UserRole.RECRUITER.value, UserRole.HIRING_MANAGER.value,
    })


def test_normal_search_route_is_unaffected_by_the_new_semantic_route():
    """Section 27 - Normal Search must keep its own exact path, method, and HR_ADMIN-only RBAC."""
    route = _get_route(_NORMAL_SEARCH_PATH, "GET")
    assert route.path == _NORMAL_SEARCH_PATH
    assert _allowed_roles(route) == frozenset({UserRole.HR_ADMIN.value})


"""Query validation (section 6) - enforced at the request-schema level, the
existing FastAPI/pydantic 422 convention already used by every other
validated field in this codebase (e.g. Normal Search's `search` min_length)."""


def test_empty_query_is_rejected_by_the_request_schema():
    with pytest.raises(ValidationError):
        TalentPoolSemanticSearchRequest(query="")


def test_whitespace_only_query_is_rejected_by_the_request_schema():
    with pytest.raises(ValidationError):
        TalentPoolSemanticSearchRequest(query="   ")


def test_valid_query_is_trimmed_by_the_request_schema():
    request = TalentPoolSemanticSearchRequest(query="  senior python engineer  ")
    assert request.query == "senior python engineer"


"""Full-stack contract: router -> service -> APIResponse, with a mocked
repository layer standing in for Postgres and the embedding model."""


def _make_service_with_mocked_repos():
    resume_repo = MagicMock()
    resume_repo.semantic_search_talent_pool.return_value = ([], 0)
    resume_repo.get_canonical_skills_by_resume_ids.return_value = {}
    resume_repo.get_active_embedding_model_version.return_value = MagicMock()
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = []
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_best_composite_scores_by_candidate_ids.return_value = {}
    embedding_service = MagicMock()
    embedding_service.generate_embedding.return_value = [0.1] * 384

    return TalentPoolService(
        candidate_repo=candidate_repo,
        resume_repo=resume_repo,
        campaign_repo=MagicMock(),
        campaign_candidate_repo=campaign_candidate_repo,
        consent_repo=MagicMock(),
        encryption_service=MagicMock(),
        audit_service=MagicMock(),
        celery_task_log_service=MagicMock(),
        resume_selection_service=MagicMock(),
        skill_repo=MagicMock(),
        config_repo=None,
        embedding_service=embedding_service,
    ), resume_repo


def test_empty_result_response_matches_the_expected_contract():
    service, _ = _make_service_with_mocked_repos()

    data = service.semantic_search_candidates(query="senior python engineer", page=1, size=6)
    response = APIResponse.ok(data=data, message="Talent Pool semantic search results retrieved successfully.")

    assert response.success is True
    assert response.data.items == []
    assert response.data.total == 0
    assert response.data.page == 1
    assert response.data.size == 6


def test_response_size_is_capped_end_to_end_when_a_larger_size_is_requested():
    service, resume_repo = _make_service_with_mocked_repos()

    data = service.semantic_search_candidates(query="senior python engineer", page=1, size=100)

    assert data.size == TALENT_POOL_MAX_PAGE_SIZE
    assert resume_repo.semantic_search_talent_pool.call_args.kwargs["size"] == TALENT_POOL_MAX_PAGE_SIZE


def test_semantic_search_handler_signature_accepts_the_request_body():
    handler = _get_route(_SEMANTIC_SEARCH_PATH, "POST").endpoint
    params = inspect.signature(handler).parameters
    assert "request" in params
    assert params["request"].annotation is TalentPoolSemanticSearchRequest
