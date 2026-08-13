"""
Epic 3 Fix 5: structural RBAC verification for the 2 new audit log routes,
mirroring test_campaign_candidate_ranking_routes.py's established pattern
(this codebase has no TestClient/HTTP-level test infrastructure anywhere).
"""
from app.api.routes.audit_log_routes import router
from app.models.identity import UserRole

_SEARCH_PATH = "/audit-log"
_EXPORT_PATH = "/audit-log/export"


def _get_route(path: str):
    for route in router.routes:
        if route.path == path and "GET" in route.methods:
            return route
    raise AssertionError(f"No GET route registered for {path}")


def _allowed_roles(route) -> frozenset:
    for dependency in route.dependant.dependencies:
        call = dependency.call
        if call.__name__ == "_check" and "allowed" in call.__code__.co_freevars:
            index = call.__code__.co_freevars.index("allowed")
            return call.__closure__[index].cell_contents
    raise AssertionError(f"No require_roles(...) dependency found on {route.path}")


def test_search_route_is_registered():
    route = _get_route(_SEARCH_PATH)
    assert route.path == _SEARCH_PATH


def test_export_route_is_registered():
    route = _get_route(_EXPORT_PATH)
    assert route.path == _EXPORT_PATH


def test_search_route_is_hr_admin_only():
    allowed = _allowed_roles(_get_route(_SEARCH_PATH))
    assert allowed == frozenset({UserRole.HR_ADMIN.value})


def test_export_route_is_hr_admin_only():
    allowed = _allowed_roles(_get_route(_EXPORT_PATH))
    assert allowed == frozenset({UserRole.HR_ADMIN.value})


def test_router_is_registered_on_the_app_with_the_expected_prefix():
    import app.main as main_module

    schema = main_module.app.openapi()
    assert "/airs/audit-log" in schema["paths"]
    assert "/airs/audit-log/export" in schema["paths"]
