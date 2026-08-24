"""
M12 Step 3 - structural verification for the 3 interview-feedback routes,
matching test_interview_routes.py's exact convention (this project has no
TestClient/HTTP-level test infrastructure - inspects the actual FastAPI
route/dependency graph built at import time).
"""
from app.api.routes.interview_feedback_routes import router
from app.middleware.jwt_middleware import _PUBLIC_PATHS
from app.middleware.rbac import get_current_user
from app.models.identity import UserRole

_GET_FORM_CONTEXT_PATH = "/interviews/feedback/{token}"
_SUBMIT_PATH = "/interviews/feedback/{token}"
_GET_FEEDBACK_PATH = "/campaign-candidates/{campaign_candidate_id}/interviews/{interview_id}/feedback"


def _get_route(path: str, method: str):
    for route in router.routes:
        if route.path == path and method in route.methods:
            return route
    raise AssertionError(f"No {method} route registered for {path}")


def _depends_on_get_current_user(route) -> bool:
    return any(dep.call is get_current_user for dep in route.dependant.dependencies)


def _allowed_roles(route) -> frozenset:
    """Extracts the frozenset require_roles(...) closed over, from the route's dependant graph."""
    for dependency in route.dependant.dependencies:
        call = dependency.call
        if call.__name__ == "_check" and "allowed" in call.__code__.co_freevars:
            index = call.__code__.co_freevars.index("allowed")
            return call.__closure__[index].cell_contents
    raise AssertionError(f"No require_roles(...) dependency found on {route.path}")


# ----------------------------------------------------------------------
# Registration.
# ----------------------------------------------------------------------

def test_get_form_context_route_is_registered_as_get():
    route = _get_route(_GET_FORM_CONTEXT_PATH, "GET")
    assert route.path == _GET_FORM_CONTEXT_PATH


def test_submit_route_is_registered_as_post():
    route = _get_route(_SUBMIT_PATH, "POST")
    assert route.path == _SUBMIT_PATH


def test_get_feedback_route_is_registered_as_get():
    route = _get_route(_GET_FEEDBACK_PATH, "GET")
    assert route.path == _GET_FEEDBACK_PATH


# ----------------------------------------------------------------------
# The 2 token-gated endpoints must NOT require an authenticated user -
# interviewers have no account at all, so get_current_user could never
# be satisfied by the request they actually send.
# ----------------------------------------------------------------------

def test_get_form_context_does_not_require_authentication():
    assert not _depends_on_get_current_user(_get_route(_GET_FORM_CONTEXT_PATH, "GET"))


def test_submit_does_not_require_authentication():
    assert not _depends_on_get_current_user(_get_route(_SUBMIT_PATH, "POST"))


def test_get_form_context_is_registered_as_a_jwt_middleware_public_path():
    """
    Without this, JWTMiddleware 401s the request before it ever reaches
    the route above - the route-level check alone is not sufficient.
    The prefix covers both GET and POST since the token varies.
    """
    assert "/airs/interviews/feedback/" in _PUBLIC_PATHS


# ----------------------------------------------------------------------
# The HR/HM-facing viewing endpoint is the opposite: authenticated,
# role-gated the same way as every other interview endpoint.
# ----------------------------------------------------------------------

def test_get_feedback_requires_an_authenticated_user():
    assert _depends_on_get_current_user(_get_route(_GET_FEEDBACK_PATH, "GET"))


def test_get_feedback_allows_hiring_manager_and_hr_admin():
    allowed = _allowed_roles(_get_route(_GET_FEEDBACK_PATH, "GET"))
    assert allowed == frozenset({UserRole.HIRING_MANAGER.value, UserRole.HR_ADMIN.value})
