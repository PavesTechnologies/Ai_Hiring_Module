"""
GET /talent-pool/candidates - Talent Pool Normal Search route contract.
This project has no TestClient/HTTP-level test infrastructure anywhere
(every existing test is a MagicMock-based unit test) - this inspects the
actual FastAPI route/dependency graph built at import time (mirrors
test_talent_pool_routes.py's approach), plus a direct call through
service.search_candidates with a mocked repository layer to prove the
existing response contract ({success, message, data:{items, total, page,
size}}) is unchanged and that the default/oversized page size is capped at
TALENT_POOL_MAX_PAGE_SIZE end-to-end.
"""
import inspect
from unittest.mock import MagicMock

from app.api.routes.talent_pool_routes import router
from app.models.identity import UserRole
from app.schemas.response import APIResponse
from app.schemas.talent_pool.talent_pool_schema import TalentPoolSearchResponse
from app.services.talent_pool.talent_pool_service import TALENT_POOL_MAX_PAGE_SIZE, TalentPoolService

_SEARCH_PATH = "/talent-pool/candidates"


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


def test_search_route_is_still_registered_at_the_existing_path():
    """M13-E01 S02 - the endpoint contract requires this exact, pre-existing path to be kept."""
    route = _get_route(_SEARCH_PATH, "GET")
    assert route.path == _SEARCH_PATH


def test_search_route_allows_only_hr_admin():
    allowed = _allowed_roles(_get_route(_SEARCH_PATH, "GET"))
    assert allowed == frozenset({UserRole.HR_ADMIN.value})


def test_search_response_model_is_unchanged():
    route = _get_route(_SEARCH_PATH, "GET")
    assert TalentPoolSearchResponse.__name__ in str(route.response_model)


def test_search_route_default_size_is_capped_page_size():
    handler = _get_route(_SEARCH_PATH, "GET").endpoint
    default_size = inspect.signature(handler).parameters["size"].default
    assert default_size.default == TALENT_POOL_MAX_PAGE_SIZE


def test_search_route_still_accepts_every_legacy_query_parameter():
    """Backward compatibility (section 26/27) - none of the pre-existing parameter names were removed."""
    handler = _get_route(_SEARCH_PATH, "GET").endpoint
    params = inspect.signature(handler).parameters
    for legacy_param in ("skill", "skills", "designation", "location", "locations", "campaign_id", "page", "size"):
        assert legacy_param in params


def test_search_route_exposes_every_new_filter_parameter():
    handler = _get_route(_SEARCH_PATH, "GET").endpoint
    params = inspect.signature(handler).parameters
    for new_param in (
        "search", "designations", "degree_levels", "education_fields",
        "campaign_ids", "pipeline_stages", "experience_min", "experience_max", "score_min", "score_max",
    ):
        assert new_param in params


"""Full-stack contract: router -> service -> APIResponse, exactly the shape
the frontend already depends on, with a mocked repository layer standing in
for Postgres."""


def _make_service_with_mocked_repos():
    resume_repo = MagicMock()
    resume_repo.search_talent_pool.return_value = ([], 0)
    resume_repo.get_canonical_skills_by_resume_ids.return_value = {}
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = []
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_best_composite_scores_by_candidate_ids.return_value = {}

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
    ), resume_repo


def test_empty_result_response_matches_the_exact_existing_contract():
    service, _ = _make_service_with_mocked_repos()

    data = service.search_candidates(page=1, size=6)
    response = APIResponse.ok(data=data, message="Talent Pool candidates retrieved successfully.")

    assert response.success is True
    assert response.message == "Talent Pool candidates retrieved successfully."
    assert response.data.items == []
    assert response.data.total == 0
    assert response.data.page == 1
    assert response.data.size == 6


def test_response_size_is_capped_end_to_end_when_a_larger_size_is_requested():
    service, resume_repo = _make_service_with_mocked_repos()

    data = service.search_candidates(page=1, size=100)

    assert data.size == TALENT_POOL_MAX_PAGE_SIZE
    assert resume_repo.search_talent_pool.call_args.kwargs["size"] == TALENT_POOL_MAX_PAGE_SIZE
