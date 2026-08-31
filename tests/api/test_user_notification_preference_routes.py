"""
Epic 5 Step 3 - structural verification for the 2 notification-preference
routes, matching test_interview_routes.py's exact convention (this
project has no TestClient/HTTP-level test infrastructure - inspects the
actual FastAPI route/dependency graph built at import time).
"""
from app.api.routes.user_notification_preference_routes import router
from app.middleware.rbac import get_current_user_id

_GET_PATH = "/users/me/notification-preferences"
_PUT_PATH = "/users/me/notification-preferences/{trigger_event}"


def _get_route(path: str, method: str):
    for route in router.routes:
        if route.path == path and method in route.methods:
            return route
    raise AssertionError(f"No {method} route registered for {path}")


def _depends_on_get_current_user_id(route) -> bool:
    return any(dep.call is get_current_user_id for dep in route.dependant.dependencies)


def test_get_preferences_route_is_registered_as_get():
    route = _get_route(_GET_PATH, "GET")
    assert route.path == _GET_PATH


def test_set_preference_route_is_registered_as_put():
    route = _get_route(_PUT_PATH, "PUT")
    assert route.path == _PUT_PATH


def test_get_preferences_requires_an_authenticated_user():
    assert _depends_on_get_current_user_id(_get_route(_GET_PATH, "GET"))


def test_set_preference_requires_an_authenticated_user():
    assert _depends_on_get_current_user_id(_get_route(_PUT_PATH, "PUT"))
