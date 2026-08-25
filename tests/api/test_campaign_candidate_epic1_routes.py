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
from app.schemas.campaign.campaign_candidate_schema import BulkSendRejectionEmailRequest, RejectAtInterviewRequest

_ADVANCE_PATH = "/campaign-candidates/{campaign_candidate_id}/advance-to-interview"
_SELECT_PATH = "/campaign-candidates/{campaign_candidate_id}/select"
_REJECT_PATH = "/campaign-candidates/{campaign_candidate_id}/reject-interview"
_SEND_REJECTION_EMAIL_PATH = "/campaign-candidates/{campaign_candidate_id}/send-rejection-email"
_BULK_SEND_REJECTION_EMAIL_PATH = "/campaign-candidates/bulk-send-rejection-email"


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
# Manual "Send Rejection Email" action.
# ----------------------------------------------------------------------

def test_send_rejection_email_route_is_registered():
    route = _get_route(_SEND_REJECTION_EMAIL_PATH)
    assert route.path == _SEND_REJECTION_EMAIL_PATH


def test_send_rejection_email_allows_hiring_manager_and_hr_admin():
    allowed = _allowed_roles(_get_route(_SEND_REJECTION_EMAIL_PATH))
    assert allowed == frozenset({UserRole.HIRING_MANAGER.value, UserRole.HR_ADMIN.value})


# ----------------------------------------------------------------------
# Bulk follow-up to Send Rejection Email - same role gate as the
# single-candidate action; ownership is enforced per-id inside the
# service, not narrowed at the route.
# ----------------------------------------------------------------------

def test_bulk_send_rejection_email_route_is_registered():
    route = _get_route(_BULK_SEND_REJECTION_EMAIL_PATH)
    assert route.path == _BULK_SEND_REJECTION_EMAIL_PATH


def test_bulk_send_rejection_email_allows_hiring_manager_and_hr_admin():
    allowed = _allowed_roles(_get_route(_BULK_SEND_REJECTION_EMAIL_PATH))
    assert allowed == frozenset({UserRole.HIRING_MANAGER.value, UserRole.HR_ADMIN.value})


def test_bulk_send_rejection_email_request_accepts_an_empty_list():
    """Unlike BulkStageMoveRequest (min_length=1), an empty batch is a valid no-op here."""
    request = BulkSendRejectionEmailRequest(campaign_candidate_ids=[])
    assert request.campaign_candidate_ids == []


def test_bulk_send_rejection_email_request_rejects_over_200_ids():
    from uuid import uuid4

    with pytest.raises(ValidationError, match="200"):
        BulkSendRejectionEmailRequest(campaign_candidate_ids=[uuid4() for _ in range(201)])


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
