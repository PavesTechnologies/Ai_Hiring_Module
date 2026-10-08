"""
Security fix: RECRUITER access to interview list / reschedule / cancel is scoped
to the recruiter's own campaigns (created, uploaded to or bulk-uploaded to), the
same check as the campaign-wide calendar (_authorize_campaign_access ->
DashboardRepository.is_campaign_accessible_to_recruiter). HR_ADMIN stays
unscoped; HIRING_MANAGER keeps the own-campaign check.

HTTP-level: real routes and real InterviewScheduleService over mocked repos.
Calendar services are mocks, every email-queue function is patched out, and the
SES sender raises if anything reaches it - nothing here may send email.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.dependencies.campaign_candidate import get_interview_schedule_service
from app.enums.constants import API_PREFIX
from app.models.interview import InterviewStatus
from app.services.interview_schedule_service import InterviewScheduleService

SERVICE = "app.services.interview_schedule_service"
EMAIL_QUEUE_FUNCTIONS = (
    "queue_interview_cancelled_email",
    "queue_interview_rescheduled_email",
    "queue_interview_scheduled_email",
    "queue_pending_feedback_requests_for_round",
    "queue_interview_interviewer_cancelled_email",
    "queue_interview_interviewer_invitation_email",
    "queue_interview_interviewer_removed_email",
    "queue_interview_interviewer_rescheduled_email",
)

_current_payload = {}


def _payload(token):
    return _current_payload


def _as(user_id, role):
    _current_payload.clear()
    _current_payload.update({"user_id": user_id, "email": f"{user_id}@example.invalid", "roles": [role]})


@pytest.fixture
def client():
    from app.main import app
    with patch("app.middleware.jwt_middleware.decode_token", _payload), \
            patch("app.middleware.rbac._ensure_local_user"), \
            patch("app.services.notifications.ses_email_client.SESEmailClient.send_email",
                  side_effect=AssertionError("email sent")):
        app.dependency_overrides[get_db] = lambda: MagicMock()
        yield app, TestClient(app, base_url=f"http://test{API_PREFIX}", client=("127.0.0.1", 50000),
                              raise_server_exceptions=False)
    app.dependency_overrides.clear()


@pytest.fixture
def email_queues():
    patchers = {name: patch(f"{SERVICE}.{name}") for name in EMAIL_QUEUE_FUNCTIONS}
    mocks = {name: p.start() for name, p in patchers.items()}
    yield mocks
    for p in patchers.values():
        p.stop()


def _env():
    campaign = SimpleNamespace(id=uuid4(), hiring_manager_id="hm-1")
    campaign_candidate = SimpleNamespace(id=uuid4(), campaign_id=campaign.id)
    start = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)
    schedule = SimpleNamespace(
        id=uuid4(), campaign_candidate_id=campaign_candidate.id, round_number=1, status=InterviewStatus.SCHEDULED,
        interview_type="Technical", start_at=start, end_at=start, timezone="UTC", candidate_timezone=None,
        platform=None, location=None, notes=None, cancel_reason=None, meeting_link=None,
        external_calendar_event_id=None, scheduled_by="hm-1", scheduled_by_role="HIRING_MANAGER",
        created_at=start, updated_at=start,
    )
    cc_repo = MagicMock()
    cc_repo.get_by_id.return_value = campaign_candidate
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    campaign_repo.get_user_names.return_value = {}
    repo = MagicMock()
    repo.get_by_id.return_value = schedule
    repo.get_all_by_campaign_candidate_id.return_value = [schedule]
    repo.replace_interviewers.return_value = ([], [])
    repo.get_active_interviewers.return_value = []
    repo.get_history.return_value = []
    microsoft, google = MagicMock(), MagicMock()
    microsoft.create_event.return_value = (None, None)
    google.create_event.return_value = (None, None)
    service = InterviewScheduleService(repo, cc_repo, campaign_repo, MagicMock(), microsoft, google, MagicMock())
    return service, campaign, campaign_candidate, schedule


def _call(http, route, campaign_candidate, schedule):
    if route == "list":
        return http.get(f"/campaign-candidates/{campaign_candidate.id}/interviews", headers={"Authorization": "Bearer t"})
    if route == "reschedule":
        return http.patch(f"/interviews/{schedule.id}/reschedule", headers={"Authorization": "Bearer t"}, json={
            "interviewers": [{"name": "Alice", "email": "alice@example.invalid"}],
            "date": "2026-08-21", "start_time": "10:00:00", "end_time": "11:00:00", "timezone": "UTC",
            "reason": "candidate asked"})
    return http.patch(f"/interviews/{schedule.id}/cancel", headers={"Authorization": "Bearer t"},
                      json={"reason": "candidate withdrew"})


ROUTES = ["list", "reschedule", "cancel"]


@pytest.mark.parametrize("route", ROUTES)
def test_recruiter_on_another_campaign_gets_403_and_nothing_changes(client, email_queues, route):
    app, http = client
    service, campaign, campaign_candidate, schedule = _env()
    app.dependency_overrides[get_interview_schedule_service] = lambda: service
    _as("recruiter-2", "RECRUITER")
    with patch(f"{SERVICE}.DashboardRepository") as dashboard:
        dashboard.return_value.is_campaign_accessible_to_recruiter.return_value = False
        response = _call(http, route, campaign_candidate, schedule)

    assert response.status_code == 403
    dashboard.return_value.is_campaign_accessible_to_recruiter.assert_called_once_with("recruiter-2", campaign.id)
    assert schedule.status == InterviewStatus.SCHEDULED
    service.interview_schedule_repo.update.assert_not_called()
    service.interview_schedule_repo.add_history.assert_not_called()
    service.interview_schedule_repo.commit.assert_not_called()
    service.audit_service.log.assert_not_called()
    for calendar in (service.microsoft_calendar_service, service.google_calendar_service):
        assert not calendar.method_calls
    assert not any(m.called for m in email_queues.values())


@pytest.mark.parametrize("route", ROUTES)
def test_recruiter_on_their_own_campaign_still_works(client, email_queues, route):
    app, http = client
    service, campaign, campaign_candidate, schedule = _env()
    app.dependency_overrides[get_interview_schedule_service] = lambda: service
    _as("recruiter-1", "RECRUITER")
    with patch(f"{SERVICE}.DashboardRepository") as dashboard:
        dashboard.return_value.is_campaign_accessible_to_recruiter.return_value = True
        response = _call(http, route, campaign_candidate, schedule)

    assert response.status_code == 200, response.text
    dashboard.return_value.is_campaign_accessible_to_recruiter.assert_called_once_with("recruiter-1", campaign.id)
    if route != "list":
        service.interview_schedule_repo.commit.assert_called_once()


@pytest.mark.parametrize("route", ROUTES)
def test_hr_admin_stays_unscoped(client, email_queues, route):
    app, http = client
    service, _, campaign_candidate, schedule = _env()
    app.dependency_overrides[get_interview_schedule_service] = lambda: service
    _as("hr-1", "HR_ADMIN")
    with patch(f"{SERVICE}.DashboardRepository") as dashboard:
        response = _call(http, route, campaign_candidate, schedule)

    assert response.status_code == 200, response.text
    dashboard.assert_not_called()


@pytest.mark.parametrize("route", ROUTES)
def test_hiring_manager_on_another_campaign_still_gets_403(client, email_queues, route):
    app, http = client
    service, _, campaign_candidate, schedule = _env()
    app.dependency_overrides[get_interview_schedule_service] = lambda: service
    _as("hm-2", "HIRING_MANAGER")
    response = _call(http, route, campaign_candidate, schedule)

    assert response.status_code == 403
    service.interview_schedule_repo.commit.assert_not_called()
