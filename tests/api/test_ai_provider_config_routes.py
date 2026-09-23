"""
Settings -> AI model routes: structural verification, same convention as
test_audit_log_routes.py (no TestClient - inspects the route/dependency
graph). Every route handles an API key or changes which model all
processing uses, so each must be HR_ADMIN-only.
"""
import pytest

from app.api.routes.ai_provider_config_routes import router
from app.models.identity import UserRole

_ROUTES = [
    ("GET", "/ai-provider-config/providers"),
    ("GET", "/ai-provider-config"),
    ("POST", "/ai-provider-config/models"),
    ("POST", "/ai-provider-config/verify"),
    ("PUT", "/ai-provider-config"),
    ("DELETE", "/ai-provider-config"),
]


def _get_route(method: str, path: str):
    for route in router.routes:
        if route.path == path and method in route.methods:
            return route
    raise AssertionError(f"No {method} route registered for {path}")


def _allowed_roles(route) -> frozenset:
    for dep in route.dependant.dependencies:
        call = dep.call
        if call.__name__ == "_check" and "allowed" in call.__code__.co_freevars:
            index = call.__code__.co_freevars.index("allowed")
            return call.__closure__[index].cell_contents
    raise AssertionError(f"No require_roles(...) dependency found on {route.path}")


@pytest.mark.parametrize("method,path", _ROUTES)
def test_route_is_hr_admin_only(method, path):
    assert _allowed_roles(_get_route(method, path)) == frozenset({UserRole.HR_ADMIN.value})
