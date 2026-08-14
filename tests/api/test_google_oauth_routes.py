"""
M12 Google Meet calendar integration - structural verification for the 3
Google OAuth routes, mirroring test_oauth_routes.py's convention exactly.
"""
from app.api.routes.google_oauth_routes import router
from app.middleware.jwt_middleware import _PUBLIC_PATHS
from app.middleware.rbac import get_current_user

_CONNECT_PATH = "/oauth/google/connect"
_CALLBACK_PATH = "/oauth/google/callback"
_STATUS_PATH = "/oauth/google/status"


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
    assert not _depends_on_get_current_user(_get_route(_CALLBACK_PATH))


def test_callback_is_registered_as_a_jwt_middleware_public_path():
    assert "/airs/oauth/google/callback" in _PUBLIC_PATHS
