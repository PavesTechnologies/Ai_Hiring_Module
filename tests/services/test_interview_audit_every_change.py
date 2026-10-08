"""
Security fix follow-up: every reschedule and cancel writes an audit entry -
actor, role, interview, campaign and which fields changed - even when the time
didn't change (a "quiet" edit of interviewers, platform, location, notes or the
meeting link). Field names only: no interviewer or candidate names/emails and no
free text (reason, location, notes) in audit_log.

Calendar services are mocks, every email-queue function is patched out, and the
SES sender raises if anything reaches it - nothing here may send email.
"""
import json
from datetime import date, datetime, time, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.enums.constants import ActionType, EntityType
from app.models.interview import InterviewPlatform, InterviewStatus
from app.schemas.interview_schema import CancelInterviewRequest, InterviewerInput, RescheduleInterviewRequest
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
START = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)
END = datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc)
PRIVATE_TEXT = ("alice@example.invalid", "Alice Interviewer", "bob@example.invalid", "Bob Interviewer",
                "Jane Candidate", "candidate's personal reason", "Room 4, Jane's office", "Jane's notes")


@pytest.fixture(autouse=True)
def no_email():
    patchers = [patch(f"{SERVICE}.{name}") for name in EMAIL_QUEUE_FUNCTIONS]
    patchers.append(patch("app.services.notifications.ses_email_client.SESEmailClient.send_email",
                          side_effect=AssertionError("email sent")))
    mocks = [p.start() for p in patchers]
    yield mocks
    for p in patchers:
        p.stop()


def _env(**schedule_overrides):
    campaign = SimpleNamespace(id=uuid4(), hiring_manager_id="hm-1")
    campaign_candidate = SimpleNamespace(id=uuid4(), campaign_id=campaign.id, full_name="Jane Candidate")
    fields = dict(
        id=uuid4(), campaign_candidate_id=campaign_candidate.id, round_number=1, status=InterviewStatus.SCHEDULED,
        interview_type="Technical", start_at=START, end_at=END, timezone="UTC", candidate_timezone=None,
        platform=None, location="Room 4, Jane's office", notes="Jane's notes", cancel_reason=None,
        meeting_link=None, external_calendar_event_id=None, scheduled_by="hm-1",
        scheduled_by_role="HIRING_MANAGER", created_at=START, updated_at=START,
    )
    fields.update(schedule_overrides)
    schedule = SimpleNamespace(**fields)
    alice = SimpleNamespace(id=uuid4(), interview_id=schedule.id, name="Alice Interviewer",
                            email="alice@example.invalid", timezone=None)

    cc_repo = MagicMock()
    cc_repo.get_by_id.return_value = campaign_candidate
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    campaign_repo.get_user_names.return_value = {}
    repo = MagicMock()
    repo.get_by_id.return_value = schedule
    repo.get_active_interviewers.return_value = [alice]
    repo.replace_interviewers.return_value = ([alice], [])
    repo.get_history.return_value = []
    microsoft, google = MagicMock(), MagicMock()
    microsoft.create_event.return_value = (None, None)
    google.create_event.return_value = (None, None)
    audit = MagicMock()
    service = InterviewScheduleService(repo, cc_repo, campaign_repo, audit, microsoft, google, MagicMock())
    return service, audit, schedule, campaign_candidate


def _reschedule(service, schedule, *, interviewers=(("Alice Interviewer", "alice@example.invalid"),),
                start=time(15, 0), end=time(16, 0), platform=None, reason=None, actor=("hm-1", "HIRING_MANAGER")):
    return service.reschedule(
        schedule.id, actor_id=actor[0], actor_roles=[actor[1]],
        request=RescheduleInterviewRequest(
            interview_type="Technical",
            interviewers=[InterviewerInput(name=n, email=e) for n, e in interviewers],
            date=date(2026, 8, 20), start_time=start, end_time=end, timezone="UTC",
            platform=platform, location="Room 4, Jane's office", notes="Jane's notes", reason=reason,
        ),
    )


def _the_audit_entry(audit, campaign_candidate, action_type, actor=("hm-1", "HIRING_MANAGER")):
    audit.log.assert_called_once()
    kwargs = audit.log.call_args.kwargs
    assert kwargs["action_type"] == action_type
    assert kwargs["actor_id"] == actor[0]
    assert kwargs["actor_role"] == actor[1]
    assert kwargs["entity_type"] == EntityType.CAMPAIGN_CANDIDATE
    assert kwargs["entity_id"] == campaign_candidate.id
    assert kwargs["campaign_id"] == campaign_candidate.campaign_id
    details = kwargs["details"]
    dumped = json.dumps(details, default=str)
    for text in PRIVATE_TEXT:
        assert text not in dumped, f"personal data / free text in audit_log: {text!r}"
    return details


def test_reschedule_changing_only_the_interviewers_is_audited():
    service, audit, schedule, cc = _env()

    _reschedule(service, schedule, interviewers=(("Bob Interviewer", "bob@example.invalid"),),
                reason="candidate's personal reason")

    details = _the_audit_entry(audit, cc, ActionType.INTERVIEW_RESCHEDULED)
    assert details["interview_id"] == str(schedule.id)
    assert details["fields_changed"] == ["interviewers"]
    assert details["interviewers_added"] == 1 and details["interviewers_removed"] == 1
    assert details["time_changed"] is False
    assert details["status_before"] == details["status_after"] == "SCHEDULED"
    assert details["reason_provided"] is True
    service.interview_schedule_repo.add_history.assert_not_called()     # still a quiet edit: no history row


def test_reschedule_changing_only_the_meeting_link_is_audited():
    # Teams event creation failed at schedule time (no link, no event id); this
    # same-time, same-interviewer reschedule creates it, so only the link changes.
    service, audit, schedule, cc = _env(platform=InterviewPlatform.TEAMS)
    service.microsoft_calendar_service.create_event.return_value = ("https://teams.example.invalid/new", "evt-1")

    _reschedule(service, schedule, platform=InterviewPlatform.TEAMS)

    details = _the_audit_entry(audit, cc, ActionType.INTERVIEW_RESCHEDULED)
    assert details["fields_changed"] == ["meeting_link"]
    assert details["time_changed"] is False
    assert details["interviewers_added"] == details["interviewers_removed"] == 0
    assert "https://teams.example.invalid/new" not in json.dumps(details)   # names of fields, not values


def test_reschedule_changing_the_time_is_audited_with_the_time_fields_and_status():
    service, audit, schedule, cc = _env()

    _reschedule(service, schedule, start=time(10, 0), end=time(11, 0))

    details = _the_audit_entry(audit, cc, ActionType.INTERVIEW_RESCHEDULED)
    assert details["fields_changed"] == ["status", "start_at", "end_at"]
    assert details["time_changed"] is True
    assert details["status_after"] == "RESCHEDULED"
    assert details["old_start_at"] == START.isoformat()


def test_reschedule_that_changes_nothing_is_still_audited():
    service, audit, schedule, cc = _env()

    _reschedule(service, schedule)

    details = _the_audit_entry(audit, cc, ActionType.INTERVIEW_RESCHEDULED)
    assert details["fields_changed"] == []
    assert details["time_changed"] is False


def test_reactivating_a_cancelled_interview_is_audited_as_such():
    service, audit, schedule, cc = _env(status=InterviewStatus.CANCELLED, cancel_reason="candidate's personal reason")

    _reschedule(service, schedule)

    details = _the_audit_entry(audit, cc, ActionType.INTERVIEW_RESCHEDULED)
    assert details["reactivated_from_cancelled"] is True
    assert details["status_before"] == "CANCELLED" and details["status_after"] == "RESCHEDULED"
    assert "status" in details["fields_changed"]


def test_recruiter_reschedule_on_own_campaign_records_the_recruiter_role():
    service, audit, schedule, cc = _env()
    with patch(f"{SERVICE}.DashboardRepository") as dashboard:
        dashboard.return_value.is_campaign_accessible_to_recruiter.return_value = True
        _reschedule(service, schedule, interviewers=(("Bob Interviewer", "bob@example.invalid"),),
                    actor=("recruiter-1", "RECRUITER"))

    details = _the_audit_entry(audit, cc, ActionType.INTERVIEW_RESCHEDULED, actor=("recruiter-1", "RECRUITER"))
    assert details["fields_changed"] == ["interviewers"]


def test_cancel_is_audited_without_the_free_text_reason():
    service, audit, schedule, cc = _env()

    service.cancel(schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
                   request=CancelInterviewRequest(reason="candidate's personal reason"))

    details = _the_audit_entry(audit, cc, ActionType.INTERVIEW_CANCELLED)
    assert details["interview_id"] == str(schedule.id)
    assert details["fields_changed"] == ["status", "cancel_reason"]
    assert details["status_before"] == "SCHEDULED" and details["status_after"] == "CANCELLED"
    assert details["reason_provided"] is True
    assert schedule.cancel_reason == "candidate's personal reason"   # still kept on the row itself


def test_nothing_is_emailed_by_any_of_this(no_email):
    service, audit, schedule, cc = _env()
    _reschedule(service, schedule, interviewers=(("Bob Interviewer", "bob@example.invalid"),))
    service.cancel(schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
                   request=CancelInterviewRequest(reason="x"))
    ses_send = no_email[-1]
    ses_send.assert_not_called()
