"""
M13-E01 S01 - structural RBAC verification for the Talent Pool routes. This
project has no TestClient/HTTP-level test infrastructure anywhere (every
existing test is a MagicMock-based unit test) - this inspects the actual
FastAPI route/dependency graph built at import time, the same graph a real
request would be dispatched through, mirroring
tests/api/test_campaign_candidate_ranking_routes.py's exact approach.
"""
from app.api.routes.talent_pool_routes import router
from app.models.identity import UserRole

_PROFILE_PATH = "/talent-pool/candidates/{candidate_id}"
_ADD_TO_CAMPAIGN_PATH = "/talent-pool/candidates/{candidate_id}/add-to-campaign"


def _get_route(path: str, method: str):
    for route in router.routes:
        if route.path == path and method in route.methods:
            return route
    raise AssertionError(f"No {method} route registered for {path}")


def _allowed_roles(route) -> frozenset:
    """Extracts the frozenset require_roles(...) closed over, from the route's dependant graph."""
    for dependency in route.dependant.dependencies:
        call = dependency.call
        if call.__name__ == "_check" and "allowed" in call.__code__.co_freevars:
            index = call.__code__.co_freevars.index("allowed")
            return call.__closure__[index].cell_contents
    raise AssertionError(f"No require_roles(...) dependency found on {route.path}")


def test_profile_route_is_registered():
    route = _get_route(_PROFILE_PATH, "GET")
    assert route.path == _PROFILE_PATH


def test_profile_route_allows_only_hr_admin():
    allowed = _allowed_roles(_get_route(_PROFILE_PATH, "GET"))
    assert allowed == frozenset({UserRole.HR_ADMIN.value})


def test_add_to_campaign_route_is_registered():
    route = _get_route(_ADD_TO_CAMPAIGN_PATH, "POST")
    assert route.path == _ADD_TO_CAMPAIGN_PATH


def test_add_to_campaign_route_allows_only_hr_admin():
    allowed = _allowed_roles(_get_route(_ADD_TO_CAMPAIGN_PATH, "POST"))
    assert allowed == frozenset({UserRole.HR_ADMIN.value})


def test_profile_response_model_is_talent_pool_candidate_profile():
    from app.schemas.talent_pool.talent_pool_schema import TalentPoolCandidateProfileResponse

    route = _get_route(_PROFILE_PATH, "GET")
    assert TalentPoolCandidateProfileResponse.__name__ in str(route.response_model)


def test_add_to_campaign_response_model_is_add_candidate_response():
    from app.schemas.talent_pool.talent_pool_schema import AddCandidateToCampaignResponse

    route = _get_route(_ADD_TO_CAMPAIGN_PATH, "POST")
    assert AddCandidateToCampaignResponse.__name__ in str(route.response_model)
