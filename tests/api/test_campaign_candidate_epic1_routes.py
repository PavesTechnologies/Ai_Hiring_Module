"""
Epic 1 (Pipeline Stage Management) - structural RBAC verification for the 3
new HM-facing routes (advance-to-interview/select/reject-interview), plus
schema-layer validation for RejectAtInterviewRequest.decision_reason. This
project has no TestClient/HTTP-level test infrastructure anywhere (every
existing test is a MagicMock-based unit test) - this inspects the actual
FastAPI route/dependency graph built at import time, same as
test_campaign_candidate_ranking_routes.py, rather than spinning up a new
test category.
"""
import pytest
from pydantic import ValidationError

from app.api.routes.campaign_candidate import router
from app.models.identity import UserRole
from app.schemas.campaign.campaign_candidate_schema import RejectAtInterviewRequest

_ADVANCE_PATH = "/campaign-candidates/{campaign_candidate_id}/advance-to-interview"
_SELECT_PATH = "/campaign-candidates/{campaign_candidate_id}/select"
_REJECT_PATH = "/campaign-candidates/{campaign_candidate_id}/reject-interview"


def _get_route(path: str):
    for route in router.routes:
        if route.path == path and "POST" in route.methods:
            return route
    raise AssertionError(f"No POST route registered for {path}")


def _allowed_roles(route) -> frozenset:
    """Extracts the frozenset require_roles(...) closed over, from the route's dependant graph."""
    for dependency in route.dependant.dependencies:
        call = dependency.call
        if call.__name__ == "_check" and "allowed" in call.__code__.co_freevars:
            index = call.__code__.co_freevars.index("allowed")
            return call.__closure__[index].cell_contents
    raise AssertionError(f"No require_roles(...) dependency found on {route.path}")


# ----------------------------------------------------------------------
# Route registration + role gating.
# ----------------------------------------------------------------------

def test_advance_to_interview_route_is_registered():
    route = _get_route(_ADVANCE_PATH)
    assert route.path == _ADVANCE_PATH


def test_select_route_is_registered():
    route = _get_route(_SELECT_PATH)
    assert route.path == _SELECT_PATH


def test_reject_interview_route_is_registered():
    route = _get_route(_REJECT_PATH)
    assert route.path == _REJECT_PATH


def test_advance_to_interview_allows_hiring_manager_and_hr_admin():
    allowed = _allowed_roles(_get_route(_ADVANCE_PATH))
    assert allowed == frozenset({UserRole.HIRING_MANAGER.value, UserRole.HR_ADMIN.value})


def test_select_allows_hiring_manager_and_hr_admin():
    allowed = _allowed_roles(_get_route(_SELECT_PATH))
    assert allowed == frozenset({UserRole.HIRING_MANAGER.value, UserRole.HR_ADMIN.value})


def test_reject_interview_route_gate_admits_both_roles_leaving_the_actual_rejection_to_transition():
    """
    The route gate deliberately admits HR_ADMIN too, even though
    allowed_transitions restricts INTERVIEW->REJECTED to HIRING_MANAGER
    only - see reject_at_interview's docstring in campaign_candidate_service.py
    and the route's own description for why: an HR_ADMIN caller is meant to
    be rejected by StageTransitionService.transition()'s own role check
    (ForbiddenPipelineRoleException -> 403), not by a route-level 403, so
    the gate here must NOT be HIRING_MANAGER-only.
    """
    allowed = _allowed_roles(_get_route(_REJECT_PATH))
    assert allowed == frozenset({UserRole.HIRING_MANAGER.value, UserRole.HR_ADMIN.value})


# ----------------------------------------------------------------------
# RejectAtInterviewRequest.decision_reason validation - enforced at
# deserialization time, before any endpoint/service code ever runs.
# ----------------------------------------------------------------------

def test_decision_reason_over_500_words_is_rejected_at_the_schema_layer():
    over_limit = " ".join(["word"] * 501)

    with pytest.raises(ValidationError, match="500 words or fewer"):
        RejectAtInterviewRequest(decision_reason=over_limit)


def test_decision_reason_at_exactly_500_words_is_accepted():
    exactly_limit = " ".join(["word"] * 500)

    request = RejectAtInterviewRequest(decision_reason=exactly_limit)

    assert request.decision_reason == exactly_limit


def test_decision_reason_empty_string_is_rejected_at_the_schema_layer():
    with pytest.raises(ValidationError, match="must not be empty"):
        RejectAtInterviewRequest(decision_reason="")


def test_decision_reason_whitespace_only_is_rejected_at_the_schema_layer():
    with pytest.raises(ValidationError, match="must not be empty"):
        RejectAtInterviewRequest(decision_reason="   \n\t  ")
