"""
M10-E03 Phase 1: structural RBAC verification for the ranked candidate list
and summary routes. This project has no TestClient/HTTP-level test
infrastructure anywhere (every existing test is a MagicMock-based unit
test), so this inspects the actual FastAPI route/dependency graph built at
import time - the same graph a real request would be dispatched through -
rather than spinning up a new test category.
"""
from app.api.routes.campaign_candidate import router
from app.models.identity import UserRole

_RANKED_LIST_PATH = "/campaign-candidates/campaign/{campaign_id}"
_SUMMARY_PATH = "/campaign-candidates/campaign/{campaign_id}/summary"


def _get_route(path: str):
    for route in router.routes:
        if route.path == path and "GET" in route.methods:
            return route
    raise AssertionError(f"No GET route registered for {path}")


def _allowed_roles(route) -> frozenset:
    """Extracts the frozenset require_roles(...) closed over, from the route's dependant graph."""
    for dependency in route.dependant.dependencies:
        call = dependency.call
        if call.__name__ == "_check" and "allowed" in call.__code__.co_freevars:
            index = call.__code__.co_freevars.index("allowed")
            return call.__closure__[index].cell_contents
    raise AssertionError(f"No require_roles(...) dependency found on {route.path}")


def test_ranked_candidate_list_route_is_registered_at_the_existing_path():
    """Story 1: extend the existing endpoint - never a second/new route for this."""
    route = _get_route(_RANKED_LIST_PATH)
    assert route.path == _RANKED_LIST_PATH


def test_ranked_candidate_list_route_allows_hr_admin_recruiter_hiring_manager():
    allowed = _allowed_roles(_get_route(_RANKED_LIST_PATH))
    assert allowed == frozenset({
        UserRole.HR_ADMIN.value, UserRole.RECRUITER.value, UserRole.HIRING_MANAGER.value,
    })


def test_summary_route_allows_hr_admin_recruiter_hiring_manager():
    allowed = _allowed_roles(_get_route(_SUMMARY_PATH))
    assert allowed == frozenset({
        UserRole.HR_ADMIN.value, UserRole.RECRUITER.value, UserRole.HIRING_MANAGER.value,
    })


def test_ranked_candidate_list_response_model_is_paginated_wrapper():
    """Response shape follows the existing CampaignPageResponse pagination convention."""
    from app.schemas.campaign.campaign_candidate_schema import RankedCampaignCandidatesResponse

    route = _get_route(_RANKED_LIST_PATH)
    # response_model is wrapped in APIResponse[...]; the inner type is what we extended.
    assert RankedCampaignCandidatesResponse.__name__ in str(route.response_model)
