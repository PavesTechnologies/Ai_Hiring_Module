"""
Structural RBAC verification for the /candidates routes - this project has
no TestClient/HTTP-level test infrastructure (every existing test is a
MagicMock-based unit test); this inspects the actual FastAPI route/
dependency graph built at import time, mirroring
tests/api/test_talent_pool_routes.py's exact approach.
"""
from app.api.routes.candidate_routes import router
from app.models.identity import UserRole

_LIST_PATH = "/candidates"
_CAMPAIGN_HISTORY_PATH = "/candidates/{candidate_id}/campaign-history"
_ERASE_PATH = "/candidates/{candidate_id}"


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


def test_list_candidates_route_is_registered():
    route = _get_route(_LIST_PATH, "GET")
    assert route.path == _LIST_PATH


def test_list_candidates_route_allows_only_hr_admin():
    allowed = _allowed_roles(_get_route(_LIST_PATH, "GET"))
    assert allowed == frozenset({UserRole.HR_ADMIN.value})


def test_list_candidates_response_model_is_candidate_directory_response():
    from app.schemas.candidate.candidate_directory_schema import CandidateDirectoryResponse

    route = _get_route(_LIST_PATH, "GET")
    assert CandidateDirectoryResponse.__name__ in str(route.response_model)


def test_list_candidates_route_has_no_path_params():
    """No campaign_id or candidate_id path param - a bare GET /candidates."""
    route = _get_route(_LIST_PATH, "GET")
    assert "{" not in route.path


def test_existing_campaign_history_route_is_unaffected():
    route = _get_route(_CAMPAIGN_HISTORY_PATH, "GET")
    assert route.path == _CAMPAIGN_HISTORY_PATH


def test_existing_erase_route_is_unaffected():
    route = _get_route(_ERASE_PATH, "DELETE")
    assert route.path == _ERASE_PATH
