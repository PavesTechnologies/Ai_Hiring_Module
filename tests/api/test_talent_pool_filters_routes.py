"""
GET /talentpoolfilters - route registration/RBAC for the Talent Pool
filter-options endpoint. Mirrors test_talent_pool_routes.py's structural
approach (no TestClient/HTTP infrastructure exists in this project - every
existing test inspects the actual FastAPI route/dependency graph built at
import time), plus a direct invocation of the underlying require_roles(...)
dependency to prove the three allowed roles pass and an unauthorized role
is actually rejected with 403 - not just that the closure was built with
the right role set.
"""
from unittest.mock import MagicMock

import pytest

from app.api.routes.talent_pool_routes import filters_router
from app.exception_handler.exceptions import ForbiddenError
from app.middleware.rbac import require_roles
from app.models.identity import UserRole
from app.schemas.talent_pool.talent_pool_schema import TalentPoolFiltersResponse

_FILTERS_PATH = "/talentpoolfilters"


def _get_route(path: str, method: str):
    for route in filters_router.routes:
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


def test_filters_route_is_registered():
    route = _get_route(_FILTERS_PATH, "GET")
    assert route.path == _FILTERS_PATH


def test_filters_route_allows_exactly_hr_admin_recruiter_hiring_manager():
    allowed = _allowed_roles(_get_route(_FILTERS_PATH, "GET"))
    assert allowed == frozenset({
        UserRole.HR_ADMIN.value,
        UserRole.RECRUITER.value,
        UserRole.HIRING_MANAGER.value,
    })


def test_filters_response_model_is_talent_pool_filters_response():
    route = _get_route(_FILTERS_PATH, "GET")
    assert TalentPoolFiltersResponse.__name__ in str(route.response_model)


# ── Direct RBAC invocation — proves 403 actually happens, not just that ────
# ── the frozenset was built correctly.                                  ───

class _FakeState:
    def __init__(self, payload):
        self.token_payload = payload


class _FakeRequest:
    def __init__(self, payload):
        self.state = _FakeState(payload)


def _invoke_rbac(role: str):
    check = require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)
    db = MagicMock()
    db.get.return_value = MagicMock()  # local user already provisioned — skip the provisioning path
    request = _FakeRequest({"user_id": "user-1", "email": "user@example.com", "roles": [role]})
    return check(request, db)


@pytest.mark.parametrize("role", ["HR_ADMIN", "RECRUITER", "HIRING_MANAGER"])
def test_each_allowed_role_passes_rbac(role):
    user = _invoke_rbac(role)
    assert role in user.roles


def test_unauthorized_role_is_rejected_with_403():
    with pytest.raises(ForbiddenError):
        _invoke_rbac("GUEST")
