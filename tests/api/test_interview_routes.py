"""
Epic 4 (M12) Step 3 - structural RBAC verification for the 3 interview
routes, matching test_campaign_candidate_epic1_routes.py's exact
convention (this project has no TestClient/HTTP-level test
infrastructure - inspects the actual FastAPI route/dependency graph
built at import time).
"""
import pytest
from pydantic import ValidationError

from app.api.routes.interview_routes import router
from app.models.identity import UserRole
from app.schemas.interview_schema import CancelInterviewRequest, InterviewerInput, ScheduleInterviewRequest

_GET_CURRENT_PATH = "/campaign-candidates/{campaign_candidate_id}/interviews"
_SCHEDULE_PATH = "/campaign-candidates/{campaign_candidate_id}/interviews"
_RESCHEDULE_PATH = "/interviews/{interview_id}/reschedule"
_CANCEL_PATH = "/interviews/{interview_id}/cancel"
_REQUEST_FEEDBACK_PATH = "/interviews/{interview_id}/request-feedback"
_COMPLETE_PATH = "/interviews/{interview_id}/complete"
_CAMPAIGN_INTERVIEWS_PATH = "/campaigns/{campaign_id}/interviews"


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


# ----------------------------------------------------------------------
# Route registration + role gating - HIRING_MANAGER (own campaign only)
# or HR_ADMIN on all 4, matching Epic 1's advance-to-interview/select.
# ----------------------------------------------------------------------

def test_get_current_route_is_registered_as_get():
    route = _get_route(_GET_CURRENT_PATH, "GET")
    assert route.path == _GET_CURRENT_PATH


def test_get_current_allows_hiring_manager_and_hr_admin():
    allowed = _allowed_roles(_get_route(_GET_CURRENT_PATH, "GET"))
    assert allowed == frozenset({UserRole.HIRING_MANAGER.value, UserRole.HR_ADMIN.value})


def test_schedule_route_is_registered_as_post():
    route = _get_route(_SCHEDULE_PATH, "POST")
    assert route.path == _SCHEDULE_PATH


def test_reschedule_route_is_registered_as_patch():
    route = _get_route(_RESCHEDULE_PATH, "PATCH")
    assert route.path == _RESCHEDULE_PATH


def test_cancel_route_is_registered_as_patch():
    route = _get_route(_CANCEL_PATH, "PATCH")
    assert route.path == _CANCEL_PATH


def test_schedule_allows_hiring_manager_and_hr_admin():
    allowed = _allowed_roles(_get_route(_SCHEDULE_PATH, "POST"))
    assert allowed == frozenset({UserRole.HIRING_MANAGER.value, UserRole.HR_ADMIN.value})


def test_reschedule_allows_hiring_manager_and_hr_admin():
    allowed = _allowed_roles(_get_route(_RESCHEDULE_PATH, "PATCH"))
    assert allowed == frozenset({UserRole.HIRING_MANAGER.value, UserRole.HR_ADMIN.value})


def test_cancel_allows_hiring_manager_and_hr_admin():
    allowed = _allowed_roles(_get_route(_CANCEL_PATH, "PATCH"))
    assert allowed == frozenset({UserRole.HIRING_MANAGER.value, UserRole.HR_ADMIN.value})


# ----------------------------------------------------------------------
# Schema-layer validation.
# ----------------------------------------------------------------------

def test_schedule_request_rejects_end_time_not_after_start_time():
    with pytest.raises(ValidationError, match="end_time must be after start_time"):
        ScheduleInterviewRequest(
            interviewers=[InterviewerInput(name="Alice", email="alice@example.com")],
            date="2026-08-20", start_time="15:00", end_time="15:00", timezone="UTC",
        )


def test_schedule_request_rejects_empty_interviewers_list():
    with pytest.raises(ValidationError):
        ScheduleInterviewRequest(
            interviewers=[], date="2026-08-20", start_time="15:00", end_time="16:00", timezone="UTC",
        )


def test_schedule_request_accepts_the_confirmed_wire_contract_shape():
    request = ScheduleInterviewRequest(
        interview_type="Technical",
        interviewers=[{"name": "Alice", "email": "alice@example.com"}],
        date="2026-08-20", start_time="15:00", end_time="16:00", timezone="Asia/Kolkata",
        duration_minutes=60, platform="TEAMS", location=None, notes="Bring laptop",
    )
    assert request.date.isoformat() == "2026-08-20"
    assert request.start_time.isoformat() == "15:00:00"
    assert request.timezone == "Asia/Kolkata"


# ----------------------------------------------------------------------
# Timezone-discrepancy fix - `timezone` is required, validated as a real
# IANA zone name (not a raw UTC offset).
# ----------------------------------------------------------------------

def test_schedule_request_rejects_missing_timezone():
    with pytest.raises(ValidationError, match="timezone"):
        ScheduleInterviewRequest(
            interviewers=[InterviewerInput(name="Alice", email="alice@example.com")],
            date="2026-08-20", start_time="15:00", end_time="16:00",
        )


def test_schedule_request_rejects_an_unrecognized_timezone_name():
    with pytest.raises(ValidationError, match="not a recognized IANA timezone"):
        ScheduleInterviewRequest(
            interviewers=[InterviewerInput(name="Alice", email="alice@example.com")],
            date="2026-08-20", start_time="15:00", end_time="16:00", timezone="Not/A_Real_Zone",
        )


def test_schedule_request_rejects_a_raw_utc_offset_instead_of_an_iana_name():
    with pytest.raises(ValidationError, match="not a recognized IANA timezone"):
        ScheduleInterviewRequest(
            interviewers=[InterviewerInput(name="Alice", email="alice@example.com")],
            date="2026-08-20", start_time="15:00", end_time="16:00", timezone="+05:30",
        )


def test_schedule_request_accepts_a_real_iana_timezone_name():
    request = ScheduleInterviewRequest(
        interviewers=[InterviewerInput(name="Alice", email="alice@example.com")],
        date="2026-08-20", start_time="15:00", end_time="16:00", timezone="America/New_York",
    )
    assert request.timezone == "America/New_York"


def test_cancel_request_rejects_empty_reason():
    with pytest.raises(ValidationError):
        CancelInterviewRequest(reason="")


# ----------------------------------------------------------------------
# Epic 5 - request-feedback, the manual counterpart to the hourly
# feedback-request sweep. Same role gating as every other interview
# endpoint above.
# ----------------------------------------------------------------------

def test_request_feedback_route_is_registered_as_post():
    route = _get_route(_REQUEST_FEEDBACK_PATH, "POST")
    assert route.path == _REQUEST_FEEDBACK_PATH


def test_request_feedback_allows_hiring_manager_and_hr_admin():
    allowed = _allowed_roles(_get_route(_REQUEST_FEEDBACK_PATH, "POST"))
    assert allowed == frozenset({UserRole.HIRING_MANAGER.value, UserRole.HR_ADMIN.value})


# ----------------------------------------------------------------------
# Epic 5 follow-up - complete, the "Mark as Completed" action. Same role
# gating as every other interview endpoint above.
# ----------------------------------------------------------------------

def test_complete_route_is_registered_as_post():
    route = _get_route(_COMPLETE_PATH, "POST")
    assert route.path == _COMPLETE_PATH


def test_complete_allows_hiring_manager_and_hr_admin():
    allowed = _allowed_roles(_get_route(_COMPLETE_PATH, "POST"))
    assert allowed == frozenset({UserRole.HIRING_MANAGER.value, UserRole.HR_ADMIN.value})


# ----------------------------------------------------------------------
# Campaign-wide interview calendar follow-up - the first campaign-scoped
# (not campaign-candidate-scoped) interview endpoint. RECRUITER is
# additionally allowed here, unlike every other interview route above.
# ----------------------------------------------------------------------

def test_campaign_interviews_route_is_registered_as_get():
    route = _get_route(_CAMPAIGN_INTERVIEWS_PATH, "GET")
    assert route.path == _CAMPAIGN_INTERVIEWS_PATH


def test_campaign_interviews_allows_hiring_manager_recruiter_and_hr_admin():
    allowed = _allowed_roles(_get_route(_CAMPAIGN_INTERVIEWS_PATH, "GET"))
    assert allowed == frozenset({UserRole.HIRING_MANAGER.value, UserRole.RECRUITER.value, UserRole.HR_ADMIN.value})
