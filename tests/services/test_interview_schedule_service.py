"""
Epic 4 (M12) Step 3 - InterviewScheduleService.schedule/reschedule/cancel.
MagicMock-based (this project's universal test-repository convention -
see test_campaign_candidate_epic1_service.py for the equivalent Epic 1
integration-style test using the same pattern), built against the real
Pydantic request schemas so the date/start_time/end_time -> start_at/end_at
combination logic is exercised end-to-end, not just the service's own
field assignments.
"""
from datetime import date, datetime, time, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.exceptions.campaign_exceptions import CampaignException
from app.models.interview import InterviewHistoryEventType, InterviewPlatform, InterviewStatus
from app.schemas.interview_schema import (
    CancelInterviewRequest,
    InterviewerInput,
    RescheduleInterviewRequest,
    ScheduleInterviewRequest,
)
from app.services.interview_schedule_service import InterviewScheduleService


def _make_campaign_candidate(campaign_id=None):
    return SimpleNamespace(id=uuid4(), campaign_id=campaign_id or uuid4())


def _make_campaign(hiring_manager_id, campaign_id=None):
    return SimpleNamespace(id=campaign_id or uuid4(), hiring_manager_id=hiring_manager_id)


def _make_schedule(status=InterviewStatus.PENDING, campaign_candidate_id=None, **overrides):
    defaults = dict(
        id=uuid4(),
        campaign_candidate_id=campaign_candidate_id or uuid4(),
        status=status,
        interview_type=None,
        start_at=None,
        end_at=None,
        platform=None,
        location=None,
        notes=None,
        cancel_reason=None,
        meeting_link=None,
        external_calendar_event_id=None,
        scheduled_by=None,
        scheduled_by_role=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_interviewer(interview_id, name="Alice", email="alice@example.com"):
    return SimpleNamespace(id=uuid4(), interview_id=interview_id, name=name, email=email)


def _schedule_request(**overrides):
    defaults = dict(
        interview_type="Technical",
        interviewers=[InterviewerInput(name="Alice", email="alice@example.com")],
        date=date(2026, 8, 20),
        start_time=time(15, 0),
        end_time=time(16, 0),
        duration_minutes=60,
        platform=InterviewPlatform.TEAMS,
        location=None,
        notes="Bring laptop",
    )
    defaults.update(overrides)
    return ScheduleInterviewRequest(**defaults)


def _make_env(campaign_candidate=None, campaign=None, schedule=None, interviewers=None, history=None):
    campaign_candidate = campaign_candidate or _make_campaign_candidate()
    campaign = campaign or _make_campaign(hiring_manager_id="hm-1", campaign_id=campaign_candidate.campaign_id)

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = campaign_candidate

    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    campaign_repo.get_user_names.return_value = {}

    interview_schedule_repo = MagicMock()
    interview_schedule_repo.get_by_campaign_candidate_id.return_value = schedule
    interview_schedule_repo.get_by_id.return_value = schedule
    interview_schedule_repo.replace_interviewers.return_value = interviewers or []
    interview_schedule_repo.get_interviewers.return_value = interviewers or []
    interview_schedule_repo.get_history.return_value = history or []

    audit_service = MagicMock()

    # Epic 4 M12 Microsoft/Google calendar follow-up: both required (see
    # InterviewScheduleService.__init__'s own comment on why). Default to
    # (None, None)/no-op, matching "not connected"/"any Graph or Calendar
    # API failure" - every pre-existing test here uses _schedule_request()'s
    # default platform=TEAMS, so microsoft_calendar_service must default to
    # something that leaves schedule.meeting_link/external_calendar_event_id
    # at None rather than raising. Accessible via
    # service.microsoft_calendar_service/service.google_calendar_service,
    # not part of this helper's return tuple, same convention as
    # interview_schedule_repo.
    microsoft_calendar_service = MagicMock()
    microsoft_calendar_service.create_event.return_value = (None, None)
    google_calendar_service = MagicMock()
    google_calendar_service.create_event.return_value = (None, None)

    service = InterviewScheduleService(
        interview_schedule_repo, campaign_candidate_repo, campaign_repo, audit_service,
        microsoft_calendar_service, google_calendar_service,
    )
    return service, campaign_candidate_repo, campaign_repo, interview_schedule_repo, audit_service, campaign_candidate


# ----------------------------------------------------------------------
# schedule() - only succeeds from PENDING.
# ----------------------------------------------------------------------

def test_schedule_happy_path_from_pending_sets_fields_and_returns_scheduled():
    campaign_candidate = _make_campaign_candidate()
    schedule = _make_schedule(status=InterviewStatus.PENDING, campaign_candidate_id=campaign_candidate.id)
    interviewer = _make_interviewer(schedule.id)
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(
        campaign_candidate=campaign_candidate, schedule=schedule, interviewers=[interviewer],
    )

    result = service.schedule(
        campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"], request=_schedule_request(),
    )

    assert schedule.status == InterviewStatus.SCHEDULED
    assert schedule.start_at == datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)
    assert schedule.end_at == datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc)
    assert schedule.scheduled_by == "hm-1"
    assert result.status == "SCHEDULED"
    assert result.history == []
    assert result.duration_minutes == 60
    repo.commit.assert_called_once()


def test_schedule_raises_409_when_status_is_not_pending():
    schedule = _make_schedule(status=InterviewStatus.SCHEDULED)
    service, *_rest, audit_service, campaign_candidate = _make_env(schedule=schedule)

    with pytest.raises(CampaignException) as exc_info:
        service.schedule(
            campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"], request=_schedule_request(),
        )

    assert exc_info.value.status_code == 409
    audit_service.log.assert_not_called()


def test_schedule_raises_409_when_no_pending_row_exists():
    service, *_rest, audit_service, campaign_candidate = _make_env(schedule=None)

    with pytest.raises(CampaignException) as exc_info:
        service.schedule(
            campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"], request=_schedule_request(),
        )

    assert exc_info.value.status_code == 409


def test_schedule_raises_404_when_campaign_candidate_not_found():
    service, cc_repo, *_rest = _make_env()
    cc_repo.get_by_id.return_value = None

    with pytest.raises(CampaignException) as exc_info:
        service.schedule(uuid4(), actor_id="hm-1", actor_roles=["HIRING_MANAGER"], request=_schedule_request())

    assert exc_info.value.status_code == 404


def test_schedule_rejects_hiring_manager_who_does_not_own_the_campaign():
    campaign_candidate = _make_campaign_candidate()
    campaign = _make_campaign(hiring_manager_id="someone-else", campaign_id=campaign_candidate.campaign_id)
    schedule = _make_schedule(status=InterviewStatus.PENDING, campaign_candidate_id=campaign_candidate.id)
    service, *_rest = _make_env(campaign_candidate=campaign_candidate, campaign=campaign, schedule=schedule)

    with pytest.raises(CampaignException) as exc_info:
        service.schedule(
            campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"], request=_schedule_request(),
        )

    assert exc_info.value.status_code == 403


def test_schedule_allows_hr_admin_regardless_of_campaign_ownership():
    campaign_candidate = _make_campaign_candidate()
    campaign = _make_campaign(hiring_manager_id="someone-else", campaign_id=campaign_candidate.campaign_id)
    schedule = _make_schedule(status=InterviewStatus.PENDING, campaign_candidate_id=campaign_candidate.id)
    service, *_rest = _make_env(campaign_candidate=campaign_candidate, campaign=campaign, schedule=schedule)

    result = service.schedule(
        campaign_candidate.id, actor_id="hr-1", actor_roles=["HR_ADMIN"], request=_schedule_request(),
    )

    assert result.status == "SCHEDULED"


def test_schedule_logs_audit_entry_with_interview_scheduled_action_type():
    from app.enums.constants import ActionType, EntityType

    campaign_candidate = _make_campaign_candidate()
    schedule = _make_schedule(status=InterviewStatus.PENDING, campaign_candidate_id=campaign_candidate.id)
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(
        campaign_candidate=campaign_candidate, schedule=schedule,
    )

    service.schedule(
        campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"], request=_schedule_request(),
    )

    audit_service.log.assert_called_once()
    call_kwargs = audit_service.log.call_args.kwargs
    assert call_kwargs["action_type"] == ActionType.INTERVIEW_SCHEDULED
    assert call_kwargs["entity_type"] == EntityType.CAMPAIGN_CANDIDATE
    assert call_kwargs["entity_id"] == campaign_candidate.id
    assert call_kwargs["actor_id"] == "hm-1"


# ----------------------------------------------------------------------
# M12 Microsoft Teams follow-up: schedule()'s calendar_service wiring.
# MicrosoftCalendarService's own not-connected/failure fail-safety is unit
# tested in test_microsoft_calendar_service.py - these prove
# InterviewScheduleService actually wires create_event's return value into
# the row and never blocks scheduling on it either way.
# ----------------------------------------------------------------------

def test_schedule_with_teams_platform_and_connected_user_populates_meeting_link_and_event_id():
    campaign_candidate = _make_campaign_candidate()
    schedule = _make_schedule(status=InterviewStatus.PENDING, campaign_candidate_id=campaign_candidate.id)
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(
        campaign_candidate=campaign_candidate, schedule=schedule,
    )
    service.microsoft_calendar_service.create_event.return_value = ("https://teams.microsoft.com/l/meetup/abc", "graph-event-1")

    result = service.schedule(
        campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=_schedule_request(platform=InterviewPlatform.TEAMS),
    )

    service.microsoft_calendar_service.create_event.assert_called_once()
    assert service.microsoft_calendar_service.create_event.call_args.args[0] == "hm-1"
    assert schedule.meeting_link == "https://teams.microsoft.com/l/meetup/abc"
    assert schedule.external_calendar_event_id == "graph-event-1"
    assert result.meeting_link == "https://teams.microsoft.com/l/meetup/abc"


def test_schedule_with_teams_platform_and_not_connected_user_still_succeeds_with_no_meeting_link():
    campaign_candidate = _make_campaign_candidate()
    schedule = _make_schedule(status=InterviewStatus.PENDING, campaign_candidate_id=campaign_candidate.id)
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(
        campaign_candidate=campaign_candidate, schedule=schedule,
    )
    service.microsoft_calendar_service.create_event.return_value = (None, None)  # not connected

    result = service.schedule(
        campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=_schedule_request(platform=InterviewPlatform.TEAMS),
    )

    assert result.status == "SCHEDULED"
    assert result.meeting_link is None
    assert schedule.external_calendar_event_id is None


def test_schedule_when_graph_call_fails_still_succeeds_with_no_meeting_link():
    """
    create_event itself never raises (see MicrosoftCalendarService) - this
    proves the service-level contract holds even under that failure,
    exactly like the not-connected case, per the explicit "never fail the
    whole scheduling request" requirement.
    """
    campaign_candidate = _make_campaign_candidate()
    schedule = _make_schedule(status=InterviewStatus.PENDING, campaign_candidate_id=campaign_candidate.id)
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(
        campaign_candidate=campaign_candidate, schedule=schedule,
    )
    service.microsoft_calendar_service.create_event.return_value = (None, None)  # Graph call failed

    result = service.schedule(
        campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=_schedule_request(platform=InterviewPlatform.TEAMS),
    )

    assert result.status == "SCHEDULED"
    assert result.meeting_link is None


def test_schedule_with_non_teams_platform_never_calls_calendar_service():
    campaign_candidate = _make_campaign_candidate()
    schedule = _make_schedule(status=InterviewStatus.PENDING, campaign_candidate_id=campaign_candidate.id)
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(
        campaign_candidate=campaign_candidate, schedule=schedule,
    )

    service.schedule(
        campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=_schedule_request(platform=InterviewPlatform.ONSITE, location="Building 4"),
    )

    service.microsoft_calendar_service.create_event.assert_not_called()
    service.google_calendar_service.create_event.assert_not_called()


# ----------------------------------------------------------------------
# M12 Google Meet follow-up: schedule()'s MEET dispatch. Mirrors the TEAMS
# block above - proves _calendar_service_for correctly routes MEET to
# google_calendar_service, not microsoft_calendar_service (the gap
# flagged explicitly in the build prompt: MEET previously fell through
# unhandled).
# ----------------------------------------------------------------------

def test_schedule_with_meet_platform_and_connected_user_populates_meeting_link_and_event_id():
    campaign_candidate = _make_campaign_candidate()
    schedule = _make_schedule(status=InterviewStatus.PENDING, campaign_candidate_id=campaign_candidate.id)
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(
        campaign_candidate=campaign_candidate, schedule=schedule,
    )
    service.google_calendar_service.create_event.return_value = ("https://meet.google.com/abc-defg-hij", "google-event-1")

    result = service.schedule(
        campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=_schedule_request(platform=InterviewPlatform.MEET),
    )

    service.google_calendar_service.create_event.assert_called_once()
    service.microsoft_calendar_service.create_event.assert_not_called()
    assert schedule.meeting_link == "https://meet.google.com/abc-defg-hij"
    assert schedule.external_calendar_event_id == "google-event-1"
    assert result.meeting_link == "https://meet.google.com/abc-defg-hij"


def test_schedule_with_meet_platform_and_not_connected_user_still_succeeds_with_no_meeting_link():
    campaign_candidate = _make_campaign_candidate()
    schedule = _make_schedule(status=InterviewStatus.PENDING, campaign_candidate_id=campaign_candidate.id)
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(
        campaign_candidate=campaign_candidate, schedule=schedule,
    )
    service.google_calendar_service.create_event.return_value = (None, None)

    result = service.schedule(
        campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=_schedule_request(platform=InterviewPlatform.MEET),
    )

    assert result.status == "SCHEDULED"
    assert result.meeting_link is None


# ----------------------------------------------------------------------
# reschedule() - only from SCHEDULED/RESCHEDULED; each call appends one
# history row (reschedule twice -> both rows persist).
# ----------------------------------------------------------------------

def test_reschedule_from_scheduled_moves_to_rescheduled_and_appends_history():
    from app.enums.constants import ActionType

    old_start = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)
    schedule = _make_schedule(status=InterviewStatus.SCHEDULED, start_at=old_start, end_at=old_start)
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(schedule=schedule)

    result = service.reschedule(
        schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=RescheduleInterviewRequest(
            interviewers=[InterviewerInput(name="Alice", email="alice@example.com")],
            date=date(2026, 8, 21), start_time=time(10, 0), end_time=time(11, 0),
            reason="candidate requested a new time",
        ),
    )

    assert schedule.status == InterviewStatus.RESCHEDULED
    assert schedule.start_at == datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc)
    repo.add_history.assert_called_once()
    history_kwargs = repo.add_history.call_args.kwargs
    assert history_kwargs["event_type"] == InterviewHistoryEventType.RESCHEDULED
    assert history_kwargs["old_start_at"] == old_start
    assert history_kwargs["new_start_at"] == datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc)
    assert history_kwargs["reason"] == "candidate requested a new time"
    audit_service.log.assert_called_once()
    assert audit_service.log.call_args.kwargs["action_type"] == ActionType.INTERVIEW_RESCHEDULED
    assert result.status == "RESCHEDULED"


def test_reschedule_twice_both_history_rows_are_reflected_in_the_response():
    """
    Each reschedule() call appends exactly one history row via add_history -
    persistence of "both rows" is InterviewScheduleHistory's append-only
    guarantee (nothing here ever updates or deletes a row). This proves the
    service reflects however many rows get_history returns, by simulating
    the second call seeing both the first and second reschedule's entries.
    """
    schedule = _make_schedule(
        status=InterviewStatus.SCHEDULED,
        start_at=datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc),
        end_at=datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc),
    )
    first_entry = SimpleNamespace(
        id=uuid4(), old_start_at=datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc),
        new_start_at=datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc),
        changed_by="hm-1", reason="first move", changed_at=datetime.now(timezone.utc),
    )
    second_entry = SimpleNamespace(
        id=uuid4(), old_start_at=datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc),
        new_start_at=datetime(2026, 8, 22, 9, 0, tzinfo=timezone.utc),
        changed_by="hm-1", reason="second move", changed_at=datetime.now(timezone.utc),
    )
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(schedule=schedule)
    repo.get_history.return_value = [first_entry, second_entry]

    result = service.reschedule(
        schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=RescheduleInterviewRequest(
            interviewers=[InterviewerInput(name="Alice", email="alice@example.com")],
            date=date(2026, 8, 22), start_time=time(9, 0), end_time=time(10, 0),
            reason="second move",
        ),
    )

    assert len(result.history) == 2
    assert result.history[0].reason == "first move"
    assert result.history[1].reason == "second move"


@pytest.mark.parametrize("status", [InterviewStatus.PENDING, InterviewStatus.CANCELLED])
def test_reschedule_blocked_unless_scheduled_or_rescheduled(status):
    schedule = _make_schedule(status=status)
    service, *_rest = _make_env(schedule=schedule)

    with pytest.raises(CampaignException) as exc_info:
        service.reschedule(
            schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
            request=RescheduleInterviewRequest(
                interviewers=[InterviewerInput(name="Alice", email="alice@example.com")],
                date=date(2026, 8, 21), start_time=time(10, 0), end_time=time(11, 0),
            ),
        )

    assert exc_info.value.status_code == 409


def test_reschedule_raises_404_when_interview_not_found():
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(schedule=None)
    repo.get_by_id.return_value = None

    with pytest.raises(CampaignException) as exc_info:
        service.reschedule(
            uuid4(), actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
            request=RescheduleInterviewRequest(
                interviewers=[InterviewerInput(name="Alice", email="alice@example.com")],
                date=date(2026, 8, 21), start_time=time(10, 0), end_time=time(11, 0),
            ),
        )

    assert exc_info.value.status_code == 404


# ----------------------------------------------------------------------
# M12 Microsoft Teams follow-up: reschedule()'s calendar_service wiring.
# ----------------------------------------------------------------------

def test_reschedule_updates_the_existing_graph_event_when_one_exists():
    schedule = _make_schedule(
        status=InterviewStatus.SCHEDULED, platform=InterviewPlatform.TEAMS,
        start_at=datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc), end_at=datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc),
        external_calendar_event_id="graph-event-1", meeting_link="https://teams.microsoft.com/l/meetup/abc",
    )
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(schedule=schedule)

    result = service.reschedule(
        schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=RescheduleInterviewRequest(
            interviewers=[InterviewerInput(name="Alice", email="alice@example.com")],
            date=date(2026, 8, 21), start_time=time(10, 0), end_time=time(11, 0), platform=InterviewPlatform.TEAMS,
        ),
    )

    service.microsoft_calendar_service.update_event.assert_called_once()
    assert service.microsoft_calendar_service.update_event.call_args.args == ("hm-1", "graph-event-1")
    service.microsoft_calendar_service.create_event.assert_not_called()
    # PATCH doesn't return a new joinUrl - the existing meeting_link is left as-is.
    assert schedule.meeting_link == "https://teams.microsoft.com/l/meetup/abc"
    assert result.meeting_link == "https://teams.microsoft.com/l/meetup/abc"


def test_reschedule_creates_a_graph_event_when_switching_to_teams_with_none_existing():
    schedule = _make_schedule(
        status=InterviewStatus.SCHEDULED, platform=InterviewPlatform.ONSITE,
        start_at=datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc), end_at=datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc),
        external_calendar_event_id=None,
    )
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(schedule=schedule)
    service.microsoft_calendar_service.create_event.return_value = ("https://teams.microsoft.com/l/meetup/new", "graph-event-2")

    result = service.reschedule(
        schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=RescheduleInterviewRequest(
            interviewers=[InterviewerInput(name="Alice", email="alice@example.com")],
            date=date(2026, 8, 21), start_time=time(10, 0), end_time=time(11, 0), platform=InterviewPlatform.TEAMS,
        ),
    )

    service.microsoft_calendar_service.create_event.assert_called_once()
    service.microsoft_calendar_service.update_event.assert_not_called()
    assert schedule.external_calendar_event_id == "graph-event-2"
    assert result.meeting_link == "https://teams.microsoft.com/l/meetup/new"


def test_reschedule_deletes_the_graph_event_when_switching_away_from_teams():
    schedule = _make_schedule(
        status=InterviewStatus.SCHEDULED, platform=InterviewPlatform.TEAMS,
        start_at=datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc), end_at=datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc),
        external_calendar_event_id="graph-event-1", meeting_link="https://teams.microsoft.com/l/meetup/abc",
    )
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(schedule=schedule)

    service.reschedule(
        schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=RescheduleInterviewRequest(
            interviewers=[InterviewerInput(name="Alice", email="alice@example.com")],
            date=date(2026, 8, 21), start_time=time(10, 0), end_time=time(11, 0), platform=InterviewPlatform.ONSITE,
        ),
    )

    service.microsoft_calendar_service.delete_event.assert_called_once_with("hm-1", "graph-event-1")
    assert schedule.meeting_link is None
    assert schedule.external_calendar_event_id is None


def test_reschedule_with_no_teams_platform_and_no_existing_event_skips_calendar_service_cleanly():
    schedule = _make_schedule(
        status=InterviewStatus.SCHEDULED, platform=InterviewPlatform.ONSITE,
        start_at=datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc), end_at=datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc),
        external_calendar_event_id=None,
    )
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(schedule=schedule)

    service.reschedule(
        schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=RescheduleInterviewRequest(
            interviewers=[InterviewerInput(name="Alice", email="alice@example.com")],
            date=date(2026, 8, 21), start_time=time(10, 0), end_time=time(11, 0), platform=InterviewPlatform.ONSITE,
        ),
    )

    service.microsoft_calendar_service.create_event.assert_not_called()
    service.microsoft_calendar_service.update_event.assert_not_called()
    service.microsoft_calendar_service.delete_event.assert_not_called()


# ----------------------------------------------------------------------
# M12 Google Meet follow-up: reschedule()'s MEET dispatch, and the new
# TEAMS<->MEET cross-provider switch cases specifically. An event in one
# provider's calendar can't be "updated" into the other's - switching
# between two different video platforms must delete the old provider's
# event and create a new one on the new provider, not attempt a same-
# provider update. This case didn't exist before a second provider did.
# ----------------------------------------------------------------------

def test_reschedule_updates_the_existing_google_event_when_staying_on_meet():
    schedule = _make_schedule(
        status=InterviewStatus.SCHEDULED, platform=InterviewPlatform.MEET,
        start_at=datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc), end_at=datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc),
        external_calendar_event_id="google-event-1", meeting_link="https://meet.google.com/abc-defg-hij",
    )
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(schedule=schedule)

    result = service.reschedule(
        schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=RescheduleInterviewRequest(
            interviewers=[InterviewerInput(name="Alice", email="alice@example.com")],
            date=date(2026, 8, 21), start_time=time(10, 0), end_time=time(11, 0), platform=InterviewPlatform.MEET,
        ),
    )

    service.google_calendar_service.update_event.assert_called_once()
    assert service.google_calendar_service.update_event.call_args.args == ("hm-1", "google-event-1")
    service.google_calendar_service.create_event.assert_not_called()
    service.microsoft_calendar_service.delete_event.assert_not_called()
    assert schedule.meeting_link == "https://meet.google.com/abc-defg-hij"
    assert result.meeting_link == "https://meet.google.com/abc-defg-hij"


def test_reschedule_from_teams_to_meet_deletes_the_teams_event_and_creates_a_google_one():
    schedule = _make_schedule(
        status=InterviewStatus.SCHEDULED, platform=InterviewPlatform.TEAMS,
        start_at=datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc), end_at=datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc),
        external_calendar_event_id="graph-event-1", meeting_link="https://teams.microsoft.com/l/meetup/abc",
    )
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(schedule=schedule)
    service.google_calendar_service.create_event.return_value = ("https://meet.google.com/xyz", "google-event-2")

    result = service.reschedule(
        schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=RescheduleInterviewRequest(
            interviewers=[InterviewerInput(name="Alice", email="alice@example.com")],
            date=date(2026, 8, 21), start_time=time(10, 0), end_time=time(11, 0), platform=InterviewPlatform.MEET,
        ),
    )

    service.microsoft_calendar_service.delete_event.assert_called_once_with("hm-1", "graph-event-1")
    service.microsoft_calendar_service.update_event.assert_not_called()
    service.google_calendar_service.create_event.assert_called_once()
    service.google_calendar_service.update_event.assert_not_called()
    assert schedule.external_calendar_event_id == "google-event-2"
    assert result.meeting_link == "https://meet.google.com/xyz"


def test_reschedule_from_meet_to_teams_deletes_the_google_event_and_creates_a_teams_one():
    schedule = _make_schedule(
        status=InterviewStatus.SCHEDULED, platform=InterviewPlatform.MEET,
        start_at=datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc), end_at=datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc),
        external_calendar_event_id="google-event-1", meeting_link="https://meet.google.com/abc-defg-hij",
    )
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(schedule=schedule)
    service.microsoft_calendar_service.create_event.return_value = ("https://teams.microsoft.com/l/meetup/new", "graph-event-2")

    result = service.reschedule(
        schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=RescheduleInterviewRequest(
            interviewers=[InterviewerInput(name="Alice", email="alice@example.com")],
            date=date(2026, 8, 21), start_time=time(10, 0), end_time=time(11, 0), platform=InterviewPlatform.TEAMS,
        ),
    )

    service.google_calendar_service.delete_event.assert_called_once_with("hm-1", "google-event-1")
    service.google_calendar_service.update_event.assert_not_called()
    service.microsoft_calendar_service.create_event.assert_called_once()
    service.microsoft_calendar_service.update_event.assert_not_called()
    assert schedule.external_calendar_event_id == "graph-event-2"
    assert result.meeting_link == "https://teams.microsoft.com/l/meetup/new"


def test_reschedule_from_onsite_to_meet_creates_a_google_event_with_nothing_to_delete():
    schedule = _make_schedule(
        status=InterviewStatus.SCHEDULED, platform=InterviewPlatform.ONSITE,
        start_at=datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc), end_at=datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc),
        external_calendar_event_id=None,
    )
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(schedule=schedule)
    service.google_calendar_service.create_event.return_value = ("https://meet.google.com/xyz", "google-event-1")

    service.reschedule(
        schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=RescheduleInterviewRequest(
            interviewers=[InterviewerInput(name="Alice", email="alice@example.com")],
            date=date(2026, 8, 21), start_time=time(10, 0), end_time=time(11, 0), platform=InterviewPlatform.MEET,
        ),
    )

    service.google_calendar_service.create_event.assert_called_once()
    service.microsoft_calendar_service.delete_event.assert_not_called()
    service.google_calendar_service.delete_event.assert_not_called()


def test_reschedule_from_meet_to_onsite_deletes_the_google_event_and_clears_both_fields():
    schedule = _make_schedule(
        status=InterviewStatus.SCHEDULED, platform=InterviewPlatform.MEET,
        start_at=datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc), end_at=datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc),
        external_calendar_event_id="google-event-1", meeting_link="https://meet.google.com/abc-defg-hij",
    )
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(schedule=schedule)

    service.reschedule(
        schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=RescheduleInterviewRequest(
            interviewers=[InterviewerInput(name="Alice", email="alice@example.com")],
            date=date(2026, 8, 21), start_time=time(10, 0), end_time=time(11, 0), platform=InterviewPlatform.ONSITE,
        ),
    )

    service.google_calendar_service.delete_event.assert_called_once_with("hm-1", "google-event-1")
    assert schedule.meeting_link is None
    assert schedule.external_calendar_event_id is None


# ----------------------------------------------------------------------
# cancel() - cancel_reason populated, notes untouched; blocked if already
# cancelled.
# ----------------------------------------------------------------------

def test_cancel_sets_cancel_reason_and_leaves_notes_untouched():
    from app.enums.constants import ActionType

    schedule = _make_schedule(
        status=InterviewStatus.SCHEDULED, notes="Bring laptop",
        start_at=datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc),
        end_at=datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc),
    )
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(schedule=schedule)

    result = service.cancel(
        schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=CancelInterviewRequest(reason="candidate withdrew"),
    )

    assert schedule.status == InterviewStatus.CANCELLED
    assert schedule.cancel_reason == "candidate withdrew"
    assert schedule.notes == "Bring laptop"
    assert result.notes == "Bring laptop"
    assert result.cancel_reason == "candidate withdrew"
    audit_service.log.assert_called_once()
    assert audit_service.log.call_args.kwargs["action_type"] == ActionType.INTERVIEW_CANCELLED


def test_cancel_raises_409_when_already_cancelled():
    schedule = _make_schedule(status=InterviewStatus.CANCELLED)
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(schedule=schedule)

    with pytest.raises(CampaignException) as exc_info:
        service.cancel(
            schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
            request=CancelInterviewRequest(reason="candidate withdrew"),
        )

    assert exc_info.value.status_code == 409
    audit_service.log.assert_not_called()


def test_cancel_raises_404_when_interview_not_found():
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(schedule=None)
    repo.get_by_id.return_value = None

    with pytest.raises(CampaignException) as exc_info:
        service.cancel(
            uuid4(), actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
            request=CancelInterviewRequest(reason="candidate withdrew"),
        )

    assert exc_info.value.status_code == 404


def test_cancel_appends_an_interview_schedule_history_row():
    """
    interview_schedule_history exists so a past decision's context is never
    lost - a cancellation is exactly that kind of decision (cancel_reason
    on the row itself would otherwise be the only record, and gets
    overwritten if the interview is ever rescheduled again later), so it
    gets a CANCELLED entry with new_start_at=null, same as reschedule's
    RESCHEDULED entry shape otherwise.
    """
    old_start = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)
    schedule = _make_schedule(status=InterviewStatus.SCHEDULED, start_at=old_start, end_at=old_start)
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(schedule=schedule)

    service.cancel(
        schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=CancelInterviewRequest(reason="candidate withdrew"),
    )

    repo.add_history.assert_called_once()
    history_kwargs = repo.add_history.call_args.kwargs
    assert history_kwargs["event_type"] == InterviewHistoryEventType.CANCELLED
    assert history_kwargs["old_start_at"] == old_start
    assert history_kwargs["new_start_at"] is None
    assert history_kwargs["reason"] == "candidate withdrew"
    assert history_kwargs["changed_by"] == "hm-1"


def test_cancel_history_entry_is_reflected_in_the_response():
    old_start = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)
    schedule = _make_schedule(status=InterviewStatus.SCHEDULED, start_at=old_start, end_at=old_start)
    cancel_entry = SimpleNamespace(
        id=uuid4(), old_start_at=old_start, new_start_at=None,
        changed_by="hm-1", reason="candidate withdrew", changed_at=datetime.now(timezone.utc),
    )
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(schedule=schedule)
    repo.get_history.return_value = [cancel_entry]

    result = service.cancel(
        schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=CancelInterviewRequest(reason="candidate withdrew"),
    )

    assert len(result.history) == 1
    assert result.history[0].new_scheduled_at is None
    assert result.history[0].reason == "candidate withdrew"


# ----------------------------------------------------------------------
# M12 Microsoft Teams follow-up: cancel()'s calendar_service wiring.
# ----------------------------------------------------------------------

def test_cancel_deletes_the_graph_event_when_one_exists():
    schedule = _make_schedule(
        status=InterviewStatus.SCHEDULED, platform=InterviewPlatform.TEAMS,
        start_at=datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc),
        end_at=datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc),
        external_calendar_event_id="graph-event-1",
    )
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(schedule=schedule)

    service.cancel(
        schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=CancelInterviewRequest(reason="candidate withdrew"),
    )

    service.microsoft_calendar_service.delete_event.assert_called_once_with("hm-1", "graph-event-1")
    # Left on the row even after a successful delete - not cleared, matching
    # "cancel doesn't scrub fields beyond what it's actually changing".
    assert schedule.external_calendar_event_id == "graph-event-1"


def test_cancel_skips_calendar_service_cleanly_when_no_event_exists():
    schedule = _make_schedule(
        status=InterviewStatus.SCHEDULED,
        start_at=datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc),
        end_at=datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc),
        external_calendar_event_id=None,
    )
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(schedule=schedule)

    service.cancel(
        schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=CancelInterviewRequest(reason="candidate withdrew"),
    )

    service.microsoft_calendar_service.delete_event.assert_not_called()
    service.google_calendar_service.delete_event.assert_not_called()


def test_cancel_deletes_the_google_event_when_platform_is_meet():
    schedule = _make_schedule(
        status=InterviewStatus.SCHEDULED, platform=InterviewPlatform.MEET,
        start_at=datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc),
        end_at=datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc),
        external_calendar_event_id="google-event-1",
    )
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(schedule=schedule)

    service.cancel(
        schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=CancelInterviewRequest(reason="candidate withdrew"),
    )

    service.google_calendar_service.delete_event.assert_called_once_with("hm-1", "google-event-1")
    service.microsoft_calendar_service.delete_event.assert_not_called()


# ----------------------------------------------------------------------
# Display-name resolution: rescheduled_by must never be a bare actor id.
# ----------------------------------------------------------------------

def test_history_entry_actor_is_resolved_to_a_display_name_not_a_bare_id():
    schedule = _make_schedule(
        status=InterviewStatus.SCHEDULED,
        start_at=datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc),
        end_at=datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc),
    )
    history_entry = SimpleNamespace(
        id=uuid4(), old_start_at=schedule.start_at, new_start_at=datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc),
        changed_by="hm-1", reason="new time", changed_at=datetime.now(timezone.utc),
    )
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(schedule=schedule)
    repo.get_history.return_value = [history_entry]
    campaign_repo.get_user_names.return_value = {"hm-1": "Priya Sharma"}

    result = service.reschedule(
        schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=RescheduleInterviewRequest(
            interviewers=[InterviewerInput(name="Alice", email="alice@example.com")],
            date=date(2026, 8, 21), start_time=time(10, 0), end_time=time(11, 0),
        ),
    )

    assert result.history[0].rescheduled_by == "Priya Sharma"


def test_history_entry_actor_falls_back_to_system_when_name_unresolved():
    schedule = _make_schedule(status=InterviewStatus.SCHEDULED, start_at=datetime.now(timezone.utc), end_at=datetime.now(timezone.utc))
    history_entry = SimpleNamespace(
        id=uuid4(), old_start_at=schedule.start_at, new_start_at=datetime.now(timezone.utc),
        changed_by="unknown-user", reason=None, changed_at=datetime.now(timezone.utc),
    )
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(schedule=schedule)
    repo.get_history.return_value = [history_entry]
    campaign_repo.get_user_names.return_value = {}

    result = service.reschedule(
        schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=RescheduleInterviewRequest(
            interviewers=[InterviewerInput(name="Alice", email="alice@example.com")],
            date=date(2026, 8, 21), start_time=time(10, 0), end_time=time(11, 0),
        ),
    )

    assert result.history[0].rescheduled_by == "System"


# ----------------------------------------------------------------------
# duration_minutes is always derived from start_at/end_at, never trusted
# from the client's (redundant) duration_minutes field.
# ----------------------------------------------------------------------

def test_duration_minutes_is_derived_from_start_and_end_at_not_the_client_value():
    campaign_candidate = _make_campaign_candidate()
    schedule = _make_schedule(status=InterviewStatus.PENDING, campaign_candidate_id=campaign_candidate.id)
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(
        campaign_candidate=campaign_candidate, schedule=schedule,
    )

    result = service.schedule(
        campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=_schedule_request(duration_minutes=999, start_time=time(15, 0), end_time=time(15, 30)),
    )

    assert result.duration_minutes == 30
