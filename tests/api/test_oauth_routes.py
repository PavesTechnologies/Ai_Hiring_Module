"""
M12 Microsoft Teams calendar integration - structural verification for the
3 OAuth routes, matching test_campaign_candidate_epic1_routes.py's
convention (this project has no TestClient/HTTP-level test
infrastructure - inspects the actual FastAPI route/dependency graph
built at import time).
"""
from app.api.routes.oauth_routes import router
from app.middleware.jwt_middleware import _PUBLIC_PATHS
from app.middleware.rbac import get_current_user

_CONNECT_PATH = "/oauth/microsoft/connect"
_CALLBACK_PATH = "/oauth/microsoft/callback"
_STATUS_PATH = "/oauth/microsoft/status"


def _get_route(path: str):
    for route in router.routes:
        if route.path == path and "GET" in route.methods:
            return route
    raise AssertionError(f"No GET route registered for {path}")


def _depends_on_get_current_user(route) -> bool:
    return any(dep.call is get_current_user for dep in route.dependant.dependencies)


def test_connect_route_is_registered():
    assert _get_route(_CONNECT_PATH).path == _CONNECT_PATH


def test_callback_route_is_registered():
    assert _get_route(_CALLBACK_PATH).path == _CALLBACK_PATH


def test_status_route_is_registered():
    assert _get_route(_STATUS_PATH).path == _STATUS_PATH


def test_connect_requires_an_authenticated_user():
    assert _depends_on_get_current_user(_get_route(_CONNECT_PATH))


def test_status_requires_an_authenticated_user():
    assert _depends_on_get_current_user(_get_route(_STATUS_PATH))


def test_callback_does_not_require_authentication():
    """
    Microsoft's redirect to this endpoint is a plain browser navigation
    with no Authorization header at all - it can never satisfy
    get_current_user, so this route must not depend on it. Security here
    comes from the signed `state` param instead (see test_oauth_state.py).
    """
    assert not _depends_on_get_current_user(_get_route(_CALLBACK_PATH))


def test_callback_is_registered_as_a_jwt_middleware_public_path():
    """
    Without this, JWTMiddleware 401s the request before it ever reaches
    the route above - the route-level check alone is not sufficient.
    """
    assert "/airs/oauth/microsoft/callback" in _PUBLIC_PATHS
