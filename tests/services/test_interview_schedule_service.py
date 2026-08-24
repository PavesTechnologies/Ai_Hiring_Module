"""
Epic 4 (M12) Step 3 - InterviewScheduleService.schedule/reschedule/cancel.
MagicMock-based (this project's universal test-repository convention -
see test_campaign_candidate_epic1_service.py for the equivalent Epic 1
integration-style test using the same pattern), built against the real
Pydantic request schemas so the date/start_time/end_time -> start_at/end_at
combination logic is exercised end-to-end, not just the service's own
field assignments.
"""
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
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
        round_number=1,
        status=status,
        interview_type=None,
        start_at=None,
        end_at=None,
        # Defaults to UTC so date/start_time/end_time as UTC wall-clock in
        # equals UTC start_at out, with zero real conversion - every
        # pre-existing test's exact-UTC-datetime assertions keep working
        # unchanged. Dedicated tests elsewhere exercise a real non-UTC
        # zone to prove _combine_to_utc's actual conversion math.
        timezone="UTC",
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
        timezone="UTC",
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
    # Multi-round follow-up: schedule() now reads the *latest* round, GET
    # reads *all* rounds - both defaulted from the same single `schedule`
    # fixture arg for every pre-existing (single-round) test, so none of
    # them need updating just because these methods now exist.
    interview_schedule_repo.get_by_campaign_candidate_id.return_value = schedule
    interview_schedule_repo.get_latest_by_campaign_candidate_id.return_value = schedule
    interview_schedule_repo.get_all_by_campaign_candidate_id.return_value = [schedule] if schedule else []
    interview_schedule_repo.get_by_id.return_value = schedule
    interview_schedule_repo.replace_interviewers.return_value = (interviewers or [], [])
    interview_schedule_repo.get_active_interviewers.return_value = interviewers or []
    interview_schedule_repo.get_history.return_value = history or []
    # Multi-round follow-up: only exercised when schedule() is called
    # against an already-SCHEDULED/RESCHEDULED/COMPLETED/CANCELLED latest
    # round ("Schedule Next Round") - a sensible default so tests that
    # don't care about round progression don't need to set this up
    # themselves.
    interview_schedule_repo.create_next_round.return_value = (
        _make_schedule(status=InterviewStatus.PENDING, round_number=2), True,
    )

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

    # Epic 5 - request_feedback()'s only collaborator beyond what's
    # already here. Accessible via service.interview_feedback_repo, not
    # part of this helper's return tuple, same convention as the 2
    # calendar services above.
    interview_feedback_repo = MagicMock()
    interview_feedback_repo.get_by_interview_schedule_id.return_value = []

    service = InterviewScheduleService(
        interview_schedule_repo, campaign_candidate_repo, campaign_repo, audit_service,
        microsoft_calendar_service, google_calendar_service, interview_feedback_repo,
    )
    return service, campaign_candidate_repo, campaign_repo, interview_schedule_repo, audit_service, campaign_candidate


# ----------------------------------------------------------------------
# schedule() - only succeeds from PENDING.
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# Timezone-discrepancy fix - _combine_to_utc's real conversion (not the
# old _combine_utc relabel), round-tripped correctly back through
# _to_response. UTC-default fixtures elsewhere in this file exercise the
# zero-offset case implicitly; these specifically prove a real, non-zero
# offset converts correctly in both directions.
# ----------------------------------------------------------------------

def test_schedule_converts_a_non_utc_timezone_to_a_real_utc_instant():
    """2:00 PM IST (UTC+5:30) must store as 8:30 AM UTC, not be relabeled as 2:00 PM UTC."""
    campaign_candidate = _make_campaign_candidate()
    schedule = _make_schedule(status=InterviewStatus.PENDING, campaign_candidate_id=campaign_candidate.id)
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(
        campaign_candidate=campaign_candidate, schedule=schedule,
    )

    service.schedule(
        campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=_schedule_request(
            date=date(2026, 8, 20), start_time=time(14, 0), end_time=time(15, 0), timezone="Asia/Kolkata",
        ),
    )

    assert schedule.start_at == datetime(2026, 8, 20, 8, 30, tzinfo=timezone.utc)
    assert schedule.end_at == datetime(2026, 8, 20, 9, 30, tzinfo=timezone.utc)
    assert schedule.timezone == "Asia/Kolkata"


def test_to_response_converts_the_stored_utc_instant_back_to_the_original_timezone():
    """Round-trip fidelity: what you scheduled (2:00 PM IST) is what you get back, not a UTC-shifted number."""
    campaign_candidate = _make_campaign_candidate()
    schedule = _make_schedule(status=InterviewStatus.PENDING, campaign_candidate_id=campaign_candidate.id)
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(
        campaign_candidate=campaign_candidate, schedule=schedule,
    )

    result = service.schedule(
        campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=_schedule_request(
            date=date(2026, 8, 20), start_time=time(14, 0), end_time=time(15, 0), timezone="Asia/Kolkata",
        ),
    )

    assert result.date == date(2026, 8, 20)
    assert result.start_time == time(14, 0)
    assert result.end_time == time(15, 0)
    assert result.timezone == "Asia/Kolkata"


def test_reschedule_converts_and_round_trips_a_non_utc_timezone_too():
    old_start = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)
    schedule = _make_schedule(status=InterviewStatus.SCHEDULED, start_at=old_start, end_at=old_start, timezone="UTC")
    service, cc_repo, campaign_repo, repo, audit_service, campaign_candidate = _make_env(schedule=schedule)

    with patch("app.services.interview_schedule_service.queue_interview_rescheduled_email"):
        result = service.reschedule(
            schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
            request=RescheduleInterviewRequest(
                interviewers=[InterviewerInput(name="Alice", email="alice@example.com")],
                date=date(2026, 8, 21), start_time=time(9, 0), end_time=time(10, 0), timezone="America/New_York",
            ),
        )

    # 9:00 AM EDT (UTC-4 in August) -> 1:00 PM UTC.
    assert schedule.start_at == datetime(2026, 8, 21, 13, 0, tzinfo=timezone.utc)
    assert schedule.timezone == "America/New_York"
    assert result.date == date(2026, 8, 21)
    assert result.start_time == time(9, 0)
    assert result.timezone == "America/New_York"


def test_get_rounds_response_timezone_is_null_for_a_pending_never_scheduled_round():
    schedule = _make_schedule(status=InterviewStatus.PENDING, start_at=None, end_at=None)
    service, *_rest = _make_env(schedule=schedule)

    result = service.get_rounds(schedule.campaign_candidate_id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert result[0].timezone is None
    assert result[0].date is None


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


# ----------------------------------------------------------------------
# Interviewer lifecycle follow-up - invitation/removal notices, queued
# after schedule()/reschedule() commit alongside the existing candidate
# email. queue_interview_interviewer_invitation_email/_removed_email
# themselves are tested directly in
# test_interview_interviewer_lifecycle_emails.py - these tests confirm
# only that schedule()/reschedule() call them with the right interviewers.
# ----------------------------------------------------------------------

def test_schedule_queues_an_invitation_email_for_each_active_interviewer():
    campaign_candidate = _make_campaign_candidate()
    schedule = _make_schedule(status=InterviewStatus.PENDING, campaign_candidate_id=campaign_candidate.id)
    alice = _make_interviewer(schedule.id, name="Alice", email="alice@example.com")
    bob = _make_interviewer(schedule.id, name="Bob", email="bob@example.com")
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(
        campaign_candidate=campaign_candidate, schedule=schedule, interviewers=[alice, bob],
    )

    with patch("app.services.interview_schedule_service.queue_interview_interviewer_invitation_email") as mock_invite:
        service.schedule(
            campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"], request=_schedule_request(),
        )

    assert mock_invite.call_count == 2
    invited = {call.args[3].email for call in mock_invite.call_args_list}
    assert invited == {"alice@example.com", "bob@example.com"}


def test_schedule_never_queues_a_removal_notice_when_nothing_was_removed():
    campaign_candidate = _make_campaign_candidate()
    schedule = _make_schedule(status=InterviewStatus.PENDING, campaign_candidate_id=campaign_candidate.id)
    interviewer = _make_interviewer(schedule.id)
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(
        campaign_candidate=campaign_candidate, schedule=schedule, interviewers=[interviewer],
    )

    with patch("app.services.interview_schedule_service.queue_interview_interviewer_removed_email") as mock_removed:
        service.schedule(
            campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"], request=_schedule_request(),
        )

    mock_removed.assert_not_called()


def test_schedule_when_latest_round_already_scheduled_starts_the_next_round_instead_of_raising():
    """
    Multi-round follow-up: calling schedule() again while the latest
    round is SCHEDULED used to be a 409 ("use reschedule instead") - it's
    now "Schedule Next Round" and must succeed. Dedicated coverage for
    the full round-progression behavior (numbering, calendar handling
    per round) lives in the "multi-round schedule()" section further
    down; this test only re-confirms the old 409 assumption no longer
    holds, and that the previous round's status is left untouched
    (status no longer means "the round is over" - see the cascading-
    cancellation follow-up).
    """
    schedule = _make_schedule(status=InterviewStatus.SCHEDULED, round_number=1)
    service, *_rest, audit_service, campaign_candidate = _make_env(schedule=schedule)

    result = service.schedule(
        campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"], request=_schedule_request(),
    )

    assert schedule.status == InterviewStatus.SCHEDULED  # never touched by scheduling the next round
    audit_service.log.assert_called_once()


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
            date=date(2026, 8, 21), start_time=time(10, 0), end_time=time(11, 0), timezone="UTC",
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


# ----------------------------------------------------------------------
# Epic 5 follow-up - is_reschedule_event: an interviewer-only (or
# platform/location/notes-only) edit with the SAME date/time is a quiet
# edit, not a reschedule event. Gates 4 things (status flip, history
# entry, audit log, candidate-facing email) on whether the time actually
# changed - calendar sync stays unconditional (tested separately, not
# duplicated here since it isn't gated at all).
# ----------------------------------------------------------------------

def test_reschedule_with_unchanged_time_and_only_interviewer_change_is_a_quiet_edit():
    """
    Same date/time, different interviewer list - must NOT flip status,
    write a history entry, write an audit entry, or tell the candidate
    their interview moved (it didn't). Interviewer replacement still
    happens - that's the entire point of this kind of edit.
    """
    from app.enums.constants import ActionType

    unchanged_start = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)
    unchanged_end = datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc)
    schedule = _make_schedule(status=InterviewStatus.SCHEDULED, start_at=unchanged_start, end_at=unchanged_end)
    service, cc_repo, campaign_repo, repo, audit_service, campaign_candidate = _make_env(schedule=schedule)

    with patch("app.services.interview_schedule_service.queue_interview_rescheduled_email") as mock_email:
        result = service.reschedule(
            schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
            request=RescheduleInterviewRequest(
                interviewers=[InterviewerInput(name="New Interviewer", email="new@example.com")],
                date=date(2026, 8, 20), start_time=time(15, 0), end_time=time(16, 0), timezone="UTC",
            ),
        )

    assert schedule.status == InterviewStatus.SCHEDULED  # never flipped to RESCHEDULED
    repo.add_history.assert_not_called()
    audit_service.log.assert_not_called()
    mock_email.assert_not_called()
    repo.replace_interviewers.assert_called_once_with(
        schedule.id, [{"name": "New Interviewer", "email": "new@example.com"}],
    )
    assert result.status == "SCHEDULED"


def test_reschedule_interviewer_swap_on_a_quiet_edit_still_queues_invitation_and_removal_notices():
    """
    Interviewer lifecycle follow-up: invitation/removal notices are
    unconditional, unlike the 4 gated reschedule-event effects above - a
    quiet edit (no time change) still needs to invite the new interviewer
    and notify the removed one.
    """
    unchanged_start = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)
    unchanged_end = datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc)
    schedule = _make_schedule(status=InterviewStatus.SCHEDULED, start_at=unchanged_start, end_at=unchanged_end)
    old_interviewer = _make_interviewer(schedule.id, name="Old Interviewer", email="old@example.com")
    new_interviewer = _make_interviewer(schedule.id, name="New Interviewer", email="new@example.com")
    service, cc_repo, campaign_repo, repo, audit_service, campaign_candidate = _make_env(schedule=schedule)
    repo.replace_interviewers.return_value = ([new_interviewer], [old_interviewer])

    with patch("app.services.interview_schedule_service.queue_interview_interviewer_invitation_email") as mock_invite, \
         patch("app.services.interview_schedule_service.queue_interview_interviewer_removed_email") as mock_removed:
        service.reschedule(
            schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
            request=RescheduleInterviewRequest(
                interviewers=[InterviewerInput(name="New Interviewer", email="new@example.com")],
                date=date(2026, 8, 20), start_time=time(15, 0), end_time=time(16, 0), timezone="UTC",
            ),
        )

    mock_invite.assert_called_once_with(cc_repo.db, campaign_candidate, schedule, new_interviewer)
    mock_removed.assert_called_once_with(cc_repo.db, campaign_candidate, schedule, old_interviewer)


def test_reschedule_with_a_genuinely_different_time_still_fires_all_four_effects():
    """No regression: a real time change behaves exactly as before this fix."""
    old_start = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)
    old_end = datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc)
    schedule = _make_schedule(status=InterviewStatus.SCHEDULED, start_at=old_start, end_at=old_end)
    service, cc_repo, campaign_repo, repo, audit_service, campaign_candidate = _make_env(schedule=schedule)

    with patch("app.services.interview_schedule_service.queue_interview_rescheduled_email") as mock_email:
        service.reschedule(
            schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
            request=RescheduleInterviewRequest(
                interviewers=[InterviewerInput(name="Alice", email="alice@example.com")],
                date=date(2026, 8, 21), start_time=time(10, 0), end_time=time(11, 0), timezone="UTC",
            ),
        )

    assert schedule.status == InterviewStatus.RESCHEDULED
    repo.add_history.assert_called_once()
    audit_service.log.assert_called_once()
    mock_email.assert_called_once()


def test_reschedule_with_both_time_and_interviewers_changed_writes_exactly_one_history_entry():
    old_start = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)
    old_end = datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc)
    schedule = _make_schedule(status=InterviewStatus.SCHEDULED, start_at=old_start, end_at=old_end)
    service, cc_repo, campaign_repo, repo, audit_service, campaign_candidate = _make_env(schedule=schedule)

    with patch("app.services.interview_schedule_service.queue_interview_rescheduled_email") as mock_email:
        service.reschedule(
            schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
            request=RescheduleInterviewRequest(
                interviewers=[InterviewerInput(name="Brand New Interviewer", email="brandnew@example.com")],
                date=date(2026, 8, 22), start_time=time(9, 0), end_time=time(10, 0), timezone="UTC",
            ),
        )

    assert schedule.status == InterviewStatus.RESCHEDULED
    repo.add_history.assert_called_once()  # exactly one, not two
    audit_service.log.assert_called_once()
    mock_email.assert_called_once()
    repo.replace_interviewers.assert_called_once_with(
        schedule.id, [{"name": "Brand New Interviewer", "email": "brandnew@example.com"}],
    )


def test_reschedule_reactivating_from_cancelled_counts_as_a_reschedule_event_even_with_the_same_time():
    """Reactivating from CANCELLED always counts as a reschedule event - the round wasn't logistically active before at all, regardless of whether the new time numerically matches the old one."""
    same_start = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)
    same_end = datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc)
    schedule = _make_schedule(
        status=InterviewStatus.CANCELLED, start_at=same_start, end_at=same_end, platform=InterviewPlatform.ONSITE,
    )
    service, cc_repo, campaign_repo, repo, audit_service, campaign_candidate = _make_env(schedule=schedule)

    with patch("app.services.interview_schedule_service.queue_interview_rescheduled_email") as mock_email:
        result = service.reschedule(
            schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
            request=RescheduleInterviewRequest(
                interviewers=[InterviewerInput(name="Alice", email="alice@example.com")],
                date=date(2026, 8, 20), start_time=time(15, 0), end_time=time(16, 0), timezone="UTC",
            ),
        )

    assert schedule.status == InterviewStatus.RESCHEDULED
    repo.add_history.assert_called_once()
    audit_service.log.assert_called_once()
    mock_email.assert_called_once()
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
            date=date(2026, 8, 22), start_time=time(9, 0), end_time=time(10, 0), timezone="UTC",
            reason="second move",
        ),
    )

    assert len(result.history) == 2
    assert result.history[0].reason == "first move"
    assert result.history[1].reason == "second move"


@pytest.mark.parametrize("status", [InterviewStatus.PENDING])
def test_reschedule_blocked_unless_scheduled_rescheduled_or_cancelled(status):
    schedule = _make_schedule(status=status)
    service, *_rest = _make_env(schedule=schedule)

    with pytest.raises(CampaignException) as exc_info:
        service.reschedule(
            schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
            request=RescheduleInterviewRequest(
                interviewers=[InterviewerInput(name="Alice", email="alice@example.com")],
                date=date(2026, 8, 21), start_time=time(10, 0), end_time=time(11, 0), timezone="UTC",
            ),
        )

    assert exc_info.value.status_code == 409


# ----------------------------------------------------------------------
# Reactivating a CANCELLED interview via reschedule() - a real gap found
# after launch: the original contract only ever specified PENDING->
# SCHEDULED, SCHEDULED|RESCHEDULED->RESCHEDULED, and any->CANCELLED, with
# nothing for CANCELLED->anything, making cancellation a dead end. Fixed
# by allowing reschedule() to accept CANCELLED too - confirmed choice,
# not schedule() (reuses the same row/history rather than a second
# "reactivate" concept).
# ----------------------------------------------------------------------

def test_reschedule_from_cancelled_reactivates_to_rescheduled_and_clears_cancel_reason():
    old_start = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)
    schedule = _make_schedule(
        status=InterviewStatus.CANCELLED, start_at=old_start, end_at=old_start,
        cancel_reason="candidate withdrew", platform=InterviewPlatform.ONSITE,
    )
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(schedule=schedule)

    result = service.reschedule(
        schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=RescheduleInterviewRequest(
            interviewers=[InterviewerInput(name="Alice", email="alice@example.com")],
            date=date(2026, 8, 25), start_time=time(10, 0), end_time=time(11, 0), timezone="UTC",
            platform=InterviewPlatform.ONSITE, reason="candidate available again",
        ),
    )

    assert schedule.status == InterviewStatus.RESCHEDULED
    assert schedule.cancel_reason is None
    assert result.status == "RESCHEDULED"
    assert result.cancel_reason is None
    repo.add_history.assert_called_once()
    history_kwargs = repo.add_history.call_args.kwargs
    assert history_kwargs["old_start_at"] == old_start
    assert history_kwargs["reason"] == "candidate available again"


def test_reschedule_from_cancelled_on_teams_creates_a_new_event_never_updates_the_deleted_one():
    """
    cancel() already deleted the Graph/Calendar event (or tried to) - a
    reactivation must never attempt to update that now-gone event, even
    when staying on the same platform. It must go through create_event,
    exactly like a fresh schedule.
    """
    old_start = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)
    schedule = _make_schedule(
        status=InterviewStatus.CANCELLED, start_at=old_start, end_at=old_start,
        platform=InterviewPlatform.TEAMS, external_calendar_event_id="graph-event-deleted",
        meeting_link="https://teams.microsoft.com/l/meetup/stale",
    )
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(schedule=schedule)
    service.microsoft_calendar_service.create_event.return_value = ("https://teams.microsoft.com/l/meetup/new", "graph-event-new")

    result = service.reschedule(
        schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=RescheduleInterviewRequest(
            interviewers=[InterviewerInput(name="Alice", email="alice@example.com")],
            date=date(2026, 8, 25), start_time=time(10, 0), end_time=time(11, 0), timezone="UTC",
            platform=InterviewPlatform.TEAMS,
        ),
    )

    service.microsoft_calendar_service.update_event.assert_not_called()
    service.microsoft_calendar_service.delete_event.assert_not_called()
    service.microsoft_calendar_service.create_event.assert_called_once()
    assert schedule.external_calendar_event_id == "graph-event-new"
    assert result.meeting_link == "https://teams.microsoft.com/l/meetup/new"


def test_reschedule_from_cancelled_to_onsite_clears_stale_meeting_link_and_event_id():
    old_start = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)
    schedule = _make_schedule(
        status=InterviewStatus.CANCELLED, start_at=old_start, end_at=old_start,
        platform=InterviewPlatform.TEAMS, external_calendar_event_id="graph-event-deleted",
        meeting_link="https://teams.microsoft.com/l/meetup/stale",
    )
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(schedule=schedule)

    result = service.reschedule(
        schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=RescheduleInterviewRequest(
            interviewers=[InterviewerInput(name="Alice", email="alice@example.com")],
            date=date(2026, 8, 25), start_time=time(10, 0), end_time=time(11, 0), timezone="UTC",
            platform=InterviewPlatform.ONSITE, location="Building A",
        ),
    )

    service.microsoft_calendar_service.update_event.assert_not_called()
    service.microsoft_calendar_service.delete_event.assert_not_called()
    service.microsoft_calendar_service.create_event.assert_not_called()
    assert schedule.meeting_link is None
    assert schedule.external_calendar_event_id is None
    assert result.meeting_link is None


def test_reschedule_raises_404_when_interview_not_found():
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(schedule=None)
    repo.get_by_id.return_value = None

    with pytest.raises(CampaignException) as exc_info:
        service.reschedule(
            uuid4(), actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
            request=RescheduleInterviewRequest(
                interviewers=[InterviewerInput(name="Alice", email="alice@example.com")],
                date=date(2026, 8, 21), start_time=time(10, 0), end_time=time(11, 0), timezone="UTC",
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
            date=date(2026, 8, 21), start_time=time(10, 0), end_time=time(11, 0), timezone="UTC", platform=InterviewPlatform.TEAMS,
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
            date=date(2026, 8, 21), start_time=time(10, 0), end_time=time(11, 0), timezone="UTC", platform=InterviewPlatform.TEAMS,
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
            date=date(2026, 8, 21), start_time=time(10, 0), end_time=time(11, 0), timezone="UTC", platform=InterviewPlatform.ONSITE,
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
            date=date(2026, 8, 21), start_time=time(10, 0), end_time=time(11, 0), timezone="UTC", platform=InterviewPlatform.ONSITE,
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
            date=date(2026, 8, 21), start_time=time(10, 0), end_time=time(11, 0), timezone="UTC", platform=InterviewPlatform.MEET,
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
            date=date(2026, 8, 21), start_time=time(10, 0), end_time=time(11, 0), timezone="UTC", platform=InterviewPlatform.MEET,
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
            date=date(2026, 8, 21), start_time=time(10, 0), end_time=time(11, 0), timezone="UTC", platform=InterviewPlatform.TEAMS,
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
            date=date(2026, 8, 21), start_time=time(10, 0), end_time=time(11, 0), timezone="UTC", platform=InterviewPlatform.MEET,
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
            date=date(2026, 8, 21), start_time=time(10, 0), end_time=time(11, 0), timezone="UTC", platform=InterviewPlatform.ONSITE,
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
# Interviewer lifecycle follow-up: cancel() previously notified only the
# candidate. Every still-active interviewer on the round now also gets a
# distinct cancellation notice.
# ----------------------------------------------------------------------

def test_cancel_queues_a_cancellation_notice_for_each_active_interviewer():
    schedule = _make_schedule(
        status=InterviewStatus.SCHEDULED,
        start_at=datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc), end_at=datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc),
    )
    alice = _make_interviewer(schedule.id, name="Alice", email="alice@example.com")
    bob = _make_interviewer(schedule.id, name="Bob", email="bob@example.com")
    service, cc_repo, campaign_repo, repo, audit_service, campaign_candidate = _make_env(
        schedule=schedule, interviewers=[alice, bob],
    )

    with patch("app.services.interview_schedule_service.queue_interview_interviewer_cancelled_email") as mock_cancel_notice:
        service.cancel(
            schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
            request=CancelInterviewRequest(reason="candidate withdrew"),
        )

    assert mock_cancel_notice.call_count == 2
    notified = {call.args[3].email for call in mock_cancel_notice.call_args_list}
    assert notified == {"alice@example.com", "bob@example.com"}
    for call in mock_cancel_notice.call_args_list:
        assert call.args[4] == "candidate withdrew"


def test_cancel_never_notifies_an_already_removed_interviewer():
    """A previously-removed (inactive) interviewer isn't involved anymore - get_active_interviewers already excludes them, so cancel() has nothing to notify."""
    schedule = _make_schedule(
        status=InterviewStatus.SCHEDULED,
        start_at=datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc), end_at=datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc),
    )
    service, cc_repo, campaign_repo, repo, audit_service, campaign_candidate = _make_env(schedule=schedule, interviewers=[])

    with patch("app.services.interview_schedule_service.queue_interview_interviewer_cancelled_email") as mock_cancel_notice:
        service.cancel(
            schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
            request=CancelInterviewRequest(reason="candidate withdrew"),
        )

    mock_cancel_notice.assert_not_called()


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
            date=date(2026, 8, 21), start_time=time(10, 0), end_time=time(11, 0), timezone="UTC",
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
            date=date(2026, 8, 21), start_time=time(10, 0), end_time=time(11, 0), timezone="UTC",
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


# ----------------------------------------------------------------------
# get_rounds() - read-only GET .../interviews. Formerly get_current(),
# returning one resource - renamed for the multi-round follow-up, now
# returns a list (one entry per round). These tests cover the
# single-round (list-of-one) case; see the dedicated multi-round section
# further down for 2/3-round list behavior.
# ----------------------------------------------------------------------

def test_get_rounds_returns_a_scheduled_interview_matching_the_schedule_response_shape():
    campaign_candidate = _make_campaign_candidate()
    schedule = _make_schedule(
        status=InterviewStatus.SCHEDULED, campaign_candidate_id=campaign_candidate.id,
        interview_type="Technical Interview", platform=InterviewPlatform.ONSITE, location="Building A",
        start_at=datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc), end_at=datetime(2026, 8, 25, 16, 0, tzinfo=timezone.utc),
    )
    interviewer = _make_interviewer(schedule.id)
    service, cc_repo, campaign_repo, repo, audit_service, _ = _make_env(
        campaign_candidate=campaign_candidate, schedule=schedule, interviewers=[interviewer],
    )

    result = service.get_rounds(campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert len(result) == 1
    assert result[0].id == schedule.id
    assert result[0].round_number == 1
    assert result[0].status == "SCHEDULED"
    assert result[0].interview_type == "Technical Interview"
    assert result[0].date.isoformat() == "2026-08-25"
    assert result[0].start_time.isoformat() == "15:00:00"
    assert result[0].duration_minutes == 60
    assert result[0].interviewers[0].email == "alice@example.com"
    assert result[0].history == []


def test_get_rounds_returns_rescheduled_interview_with_two_history_entries_in_order():
    schedule = _make_schedule(
        status=InterviewStatus.RESCHEDULED,
        start_at=datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc), end_at=datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc),
    )
    first_entry = SimpleNamespace(
        id=uuid4(), old_start_at=datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc),
        new_start_at=datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc),
        changed_by="hm-1", reason="first move", changed_at=datetime.now(timezone.utc),
    )
    second_entry = SimpleNamespace(
        id=uuid4(), old_start_at=datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc),
        new_start_at=datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc),
        changed_by="hm-1", reason="second move", changed_at=datetime.now(timezone.utc),
    )
    service, cc_repo, campaign_repo, repo, audit_service, campaign_candidate = _make_env(schedule=schedule)
    repo.get_history.return_value = [first_entry, second_entry]

    result = service.get_rounds(campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert result[0].status == "RESCHEDULED"
    assert len(result[0].history) == 2
    assert result[0].history[0].reason == "first move"
    assert result[0].history[1].reason == "second move"


def test_get_rounds_returns_pending_row_cleanly_not_an_error():
    schedule = _make_schedule(status=InterviewStatus.PENDING)  # start_at/end_at both None
    service, cc_repo, campaign_repo, repo, audit_service, campaign_candidate = _make_env(schedule=schedule)

    result = service.get_rounds(campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert result[0].status == "PENDING"
    assert result[0].date is None
    assert result[0].start_time is None
    assert result[0].end_time is None
    assert result[0].duration_minutes is None
    assert result[0].meeting_link is None
    assert result[0].history == []


def test_get_rounds_raises_404_when_no_row_exists_at_all():
    service, cc_repo, campaign_repo, repo, audit_service, campaign_candidate = _make_env(schedule=None)

    with pytest.raises(CampaignException) as exc_info:
        service.get_rounds(campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 404


def test_get_rounds_raises_404_when_campaign_candidate_not_found():
    service, cc_repo, *_rest = _make_env()
    cc_repo.get_by_id.return_value = None

    with pytest.raises(CampaignException) as exc_info:
        service.get_rounds(uuid4(), actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 404


def test_get_rounds_rejects_hiring_manager_who_does_not_own_the_campaign():
    campaign_candidate = _make_campaign_candidate()
    campaign = _make_campaign(hiring_manager_id="someone-else", campaign_id=campaign_candidate.campaign_id)
    schedule = _make_schedule(status=InterviewStatus.PENDING, campaign_candidate_id=campaign_candidate.id)
    service, *_rest = _make_env(campaign_candidate=campaign_candidate, campaign=campaign, schedule=schedule)

    with pytest.raises(CampaignException) as exc_info:
        service.get_rounds(campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 403


def test_get_rounds_allows_hr_admin_regardless_of_campaign_ownership():
    campaign_candidate = _make_campaign_candidate()
    campaign = _make_campaign(hiring_manager_id="someone-else", campaign_id=campaign_candidate.campaign_id)
    schedule = _make_schedule(status=InterviewStatus.PENDING, campaign_candidate_id=campaign_candidate.id)
    service, *_rest = _make_env(campaign_candidate=campaign_candidate, campaign=campaign, schedule=schedule)

    result = service.get_rounds(campaign_candidate.id, actor_id="hr-1", actor_roles=["HR_ADMIN"])

    assert result[0].status == "PENDING"


# ----------------------------------------------------------------------
# Multi-round redesign - "Schedule Next Round" completes the current
# round and creates the next, atomically. A stateful in-memory fake
# repository is used here (not the static MagicMock _make_env above)
# because these tests need schedule()/get_rounds() calls to see the
# effect of each other's writes, the same way the real DB would - a
# static mock can't simulate "round 2 now exists" after round 2 is
# created mid-test.
# ----------------------------------------------------------------------

def _make_stateful_env(campaign_candidate=None, campaign=None):
    campaign_candidate = campaign_candidate or _make_campaign_candidate()
    campaign = campaign or _make_campaign(hiring_manager_id="hm-1", campaign_id=campaign_candidate.campaign_id)

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = campaign_candidate

    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    campaign_repo.get_user_names.return_value = {}

    rounds_by_id = {}
    interviewers_by_round_id = {}

    def _rounds_for(campaign_candidate_id):
        return [r for r in rounds_by_id.values() if r.campaign_candidate_id == campaign_candidate_id]

    def _latest(campaign_candidate_id):
        rows = _rounds_for(campaign_candidate_id)
        return max(rows, key=lambda r: r.round_number) if rows else None

    def _all(campaign_candidate_id):
        return sorted(_rounds_for(campaign_candidate_id), key=lambda r: r.round_number)

    def _get_by_id(interview_id):
        return rounds_by_id.get(interview_id)

    def _create_next_round(campaign_candidate_id, round_number):
        existing = [
            r for r in _rounds_for(campaign_candidate_id) if r.round_number == round_number
        ]
        if existing:
            return existing[0], False
        row = _make_schedule(
            status=InterviewStatus.PENDING, campaign_candidate_id=campaign_candidate_id, round_number=round_number,
        )
        rounds_by_id[row.id] = row
        return row, True

    def _update(schedule):
        rounds_by_id[schedule.id] = schedule
        return schedule

    def _replace_interviewers(interview_id, interviewers):
        rows = [
            SimpleNamespace(id=uuid4(), interview_id=interview_id, name=i["name"], email=i["email"], is_active=True)
            for i in interviewers
        ]
        interviewers_by_round_id[interview_id] = rows
        return rows, []

    def _get_active_interviewers(interview_id):
        return interviewers_by_round_id.get(interview_id, [])

    interview_schedule_repo = MagicMock()
    interview_schedule_repo.get_latest_by_campaign_candidate_id.side_effect = _latest
    interview_schedule_repo.get_all_by_campaign_candidate_id.side_effect = _all
    interview_schedule_repo.get_by_id.side_effect = _get_by_id
    interview_schedule_repo.create_next_round.side_effect = _create_next_round
    interview_schedule_repo.update.side_effect = _update
    interview_schedule_repo.replace_interviewers.side_effect = _replace_interviewers
    interview_schedule_repo.get_active_interviewers.side_effect = _get_active_interviewers
    interview_schedule_repo.get_history.return_value = []
    interview_schedule_repo.add_history.return_value = SimpleNamespace(id=uuid4())

    microsoft_calendar_service = MagicMock()
    microsoft_calendar_service.create_event.return_value = (None, None)
    google_calendar_service = MagicMock()
    google_calendar_service.create_event.return_value = (None, None)

    service = InterviewScheduleService(
        interview_schedule_repo, campaign_candidate_repo, campaign_repo, MagicMock(),
        microsoft_calendar_service, google_calendar_service, MagicMock(),
    )

    # Seed round 1 PENDING - matching what the real Step 2 hook creates.
    round1 = _make_schedule(
        status=InterviewStatus.PENDING, campaign_candidate_id=campaign_candidate.id, round_number=1,
    )
    rounds_by_id[round1.id] = round1

    return service, campaign_candidate, interview_schedule_repo


def _round_1_request(**overrides):
    return _schedule_request(platform=InterviewPlatform.ONSITE, **overrides)


def test_round_1_schedule_behaves_identically_to_before():
    service, campaign_candidate, repo = _make_stateful_env()

    result = service.schedule(
        campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"], request=_round_1_request(),
    )

    assert result.round_number == 1
    assert result.status == "SCHEDULED"
    assert result.history == []


def test_schedule_next_round_creates_a_scheduled_round_2_leaving_round_1_untouched():
    """
    Cascading-cancellation follow-up: scheduling round 2 no longer flips
    round 1 to COMPLETED - status now only tracks a round's own
    logistics. Round 1 is only ever closed out by the candidate actually
    leaving INTERVIEW (see test_stage_transition_service.py/
    test_pipeline_transition_service.py/
    test_campaign_service_override_candidate_stage.py for that cascade).
    """
    service, campaign_candidate, repo = _make_stateful_env()
    service.schedule(campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"], request=_round_1_request())

    result = service.schedule(
        campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=_round_1_request(interview_type="Panel Round"),
    )

    assert result.round_number == 2
    assert result.status == "SCHEDULED"
    assert result.interview_type == "Panel Round"

    all_rounds = repo.get_all_by_campaign_candidate_id(campaign_candidate.id)
    assert [r.round_number for r in all_rounds] == [1, 2]
    assert [r.status for r in all_rounds] == [InterviewStatus.SCHEDULED, InterviewStatus.SCHEDULED]


def test_three_rounds_in_a_row_all_present_correctly_numbered_none_auto_completed():
    service, campaign_candidate, repo = _make_stateful_env()

    for round_label in ("Round 1", "Round 2", "Round 3"):
        service.schedule(
            campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
            request=_round_1_request(interview_type=round_label),
        )

    all_rounds = repo.get_all_by_campaign_candidate_id(campaign_candidate.id)
    assert [r.round_number for r in all_rounds] == [1, 2, 3]
    assert [r.status for r in all_rounds] == [InterviewStatus.SCHEDULED] * 3
    assert [r.interview_type for r in all_rounds] == ["Round 1", "Round 2", "Round 3"]


def test_get_rounds_returns_all_rounds_in_order_not_just_the_latest():
    service, campaign_candidate, repo = _make_stateful_env()
    for round_label in ("Round 1", "Round 2"):
        service.schedule(
            campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
            request=_round_1_request(interview_type=round_label),
        )

    result = service.get_rounds(campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert [r.round_number for r in result] == [1, 2]
    assert [r.status for r in result] == ["SCHEDULED", "SCHEDULED"]


def test_reschedule_and_cancel_remain_scoped_to_one_round_id_regardless_of_other_rounds():
    service, campaign_candidate, repo = _make_stateful_env()
    service.schedule(campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"], request=_round_1_request())
    round_2 = service.schedule(
        campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"], request=_round_1_request(),
    )

    cancel_result = service.cancel(
        round_2.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"], request=CancelInterviewRequest(reason="reschedule needed"),
    )

    assert cancel_result.round_number == 2
    assert cancel_result.status == "CANCELLED"
    round_1 = repo.get_by_id([r.id for r in repo.get_all_by_campaign_candidate_id(campaign_candidate.id) if r.round_number == 1][0])
    assert round_1.status == InterviewStatus.SCHEDULED  # untouched by round 2's cancel


def test_schedule_next_round_from_a_cancelled_latest_round_preserves_cancelled_status():
    """
    Build-prompt-confirmed behavior: COMPLETED/CANCELLED at the latest
    round still allows starting a new round (a cancelled interview being
    retried) - but the cancelled round's own status must stay CANCELLED,
    never silently relabelled COMPLETED just because schedule() was
    called again.
    """
    service, campaign_candidate, repo = _make_stateful_env()
    round_1 = service.schedule(
        campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"], request=_round_1_request(),
    )
    service.cancel(round_1.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"], request=CancelInterviewRequest(reason="no-show"))

    result = service.schedule(
        campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"], request=_round_1_request(),
    )

    assert result.round_number == 2
    assert result.status == "SCHEDULED"
    round_1_row = repo.get_by_id(round_1.id)
    assert round_1_row.status == InterviewStatus.CANCELLED  # never overwritten to COMPLETED


def test_schedule_next_round_race_returns_the_winners_row_without_modifying_it():
    """
    Two concurrent "Schedule Next Round" calls (a double-click) can both
    read the same latest round and compute the same next round_number -
    UNIQUE(campaign_candidate_id, round_number) is the real backstop;
    create_next_round's SAVEPOINT turns the loser's constraint violation
    into "return the winner's row" (was_created=False), and schedule()
    must not then try to fill in fields/create a calendar event/replace
    interviewers on a row it didn't actually create.
    """
    schedule = _make_schedule(status=InterviewStatus.SCHEDULED, round_number=1)
    winner_round_2 = _make_schedule(
        status=InterviewStatus.SCHEDULED, round_number=2, interview_type="Winner's round",
        meeting_link="https://example.com/winner",
    )
    service, cc_repo, campaign_repo, repo, audit_service, campaign_candidate = _make_env(schedule=schedule)
    repo.create_next_round.return_value = (winner_round_2, False)

    result = service.schedule(
        campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        request=_schedule_request(interview_type="My attempt"),
    )

    assert result.id == winner_round_2.id
    assert result.interview_type == "Winner's round"
    assert result.meeting_link == "https://example.com/winner"
    repo.replace_interviewers.assert_not_called()
    service.microsoft_calendar_service.create_event.assert_not_called()
    audit_service.log.assert_not_called()


# ----------------------------------------------------------------------
# Epic 5 - request_feedback(), the manual counterpart to the hourly
# feedback-request sweep. "Who still needs asking" (already gave
# feedback, already been emailed) is resolved by the shared
# queue_pending_feedback_requests_for_round() - already tested directly
# in test_interview_feedback_request_emails.py, so most tests here patch
# it and check request_feedback()'s OWN job: validation, ownership, and
# passing the right arguments through. One end-to-end test proves the
# real wiring without any patching at all.
# ----------------------------------------------------------------------

def test_request_feedback_rejects_a_round_whose_interview_has_not_started_yet():
    future_start = datetime.now(timezone.utc) + timedelta(hours=1)
    schedule = _make_schedule(status=InterviewStatus.SCHEDULED, start_at=future_start)
    service, *_rest = _make_env(schedule=schedule)

    with patch("app.services.interview_schedule_service.queue_pending_feedback_requests_for_round") as mock_queue:
        with pytest.raises(CampaignException) as exc_info:
            service.request_feedback(schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 400
    assert str(exc_info.value) == "Cannot request feedback before the interview has started."
    mock_queue.assert_not_called()


def test_request_feedback_rejects_a_round_with_no_start_at_at_all():
    schedule = _make_schedule(status=InterviewStatus.PENDING, start_at=None)
    service, *_rest = _make_env(schedule=schedule)

    with patch("app.services.interview_schedule_service.queue_pending_feedback_requests_for_round") as mock_queue:
        with pytest.raises(CampaignException) as exc_info:
            service.request_feedback(schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 400
    mock_queue.assert_not_called()


def test_request_feedback_rejects_a_cancelled_round_even_if_its_start_at_has_passed():
    """
    cancel() never clears start_at, so a CANCELLED round whose original
    start_at is in the past would otherwise sail through the start_at
    check below - this interview never happened as planned, and must be
    rejected on status alone, mirroring get_ended_active_rounds' own
    SCHEDULED/RESCHEDULED-only filter so the sweep and this manual
    trigger never diverge on what's a valid target.
    """
    past_start = datetime.now(timezone.utc) - timedelta(hours=2)
    schedule = _make_schedule(status=InterviewStatus.CANCELLED, start_at=past_start)
    service, *_rest = _make_env(schedule=schedule)

    with patch("app.services.interview_schedule_service.queue_pending_feedback_requests_for_round") as mock_queue:
        with pytest.raises(CampaignException) as exc_info:
            service.request_feedback(schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 400
    assert str(exc_info.value) == "Cannot request feedback for an interview with status CANCELLED."
    mock_queue.assert_not_called()


def test_request_feedback_rejects_a_pending_round_on_status_before_reaching_the_start_at_check():
    schedule = _make_schedule(status=InterviewStatus.PENDING, start_at=None)
    service, *_rest = _make_env(schedule=schedule)

    with pytest.raises(CampaignException) as exc_info:
        service.request_feedback(schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert str(exc_info.value) == "Cannot request feedback for an interview with status PENDING."


def test_request_feedback_queues_for_a_round_whose_interview_has_already_started():
    past_start = datetime.now(timezone.utc) - timedelta(hours=2)
    interviewer_a, interviewer_b = _make_interviewer(uuid4()), _make_interviewer(uuid4())
    schedule = _make_schedule(status=InterviewStatus.SCHEDULED, start_at=past_start)
    service, cc_repo, campaign_repo, repo, audit_service, campaign_candidate = _make_env(
        schedule=schedule, interviewers=[interviewer_a, interviewer_b],
    )

    with patch(
        "app.services.interview_schedule_service.queue_pending_feedback_requests_for_round", return_value=2,
    ) as mock_queue:
        result = service.request_feedback(schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert result.queued_count == 2
    mock_queue.assert_called_once_with(
        cc_repo.db, campaign_candidate, schedule, [interviewer_a, interviewer_b], service.interview_feedback_repo,
    )


def test_request_feedback_returns_zero_when_nothing_is_left_to_request():
    """Matches Step 4's existing dedup guarantee - clicking again immediately (or after the sweep already ran) is a valid 0, not an error."""
    past_start = datetime.now(timezone.utc) - timedelta(hours=2)
    schedule = _make_schedule(status=InterviewStatus.SCHEDULED, start_at=past_start)
    service, *_rest = _make_env(schedule=schedule, interviewers=[_make_interviewer(uuid4())])

    with patch(
        "app.services.interview_schedule_service.queue_pending_feedback_requests_for_round", return_value=0,
    ):
        result = service.request_feedback(schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert result.queued_count == 0


def test_request_feedback_rejects_a_non_owning_hiring_manager():
    past_start = datetime.now(timezone.utc) - timedelta(hours=2)
    schedule = _make_schedule(status=InterviewStatus.SCHEDULED, start_at=past_start)
    service, *_rest = _make_env(schedule=schedule)

    with patch("app.services.interview_schedule_service.queue_pending_feedback_requests_for_round") as mock_queue:
        with pytest.raises(CampaignException) as exc_info:
            service.request_feedback(schedule.id, actor_id="not-the-hm", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 403
    mock_queue.assert_not_called()


def test_request_feedback_allows_hr_admin_regardless_of_ownership():
    past_start = datetime.now(timezone.utc) - timedelta(hours=2)
    schedule = _make_schedule(status=InterviewStatus.SCHEDULED, start_at=past_start)
    service, *_rest = _make_env(schedule=schedule)

    with patch("app.services.interview_schedule_service.queue_pending_feedback_requests_for_round", return_value=0):
        result = service.request_feedback(schedule.id, actor_id="anyone", actor_roles=["HR_ADMIN"])

    assert result.queued_count == 0


def test_request_feedback_raises_404_when_interview_not_found():
    service, *_rest = _make_env(schedule=None)
    service.interview_schedule_repo.get_by_id.return_value = None

    with pytest.raises(CampaignException) as exc_info:
        service.request_feedback(uuid4(), actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 404


def test_request_feedback_end_to_end_with_one_interviewer_already_having_given_feedback():
    """
    No patching at all - proves the real wiring through
    queue_pending_feedback_requests_for_round() and
    queue_interview_feedback_requested_email() together: of 2
    interviewers, only the one without feedback gets queued.
    """
    past_start = datetime.now(timezone.utc) - timedelta(hours=2)
    fed_back, pending = _make_interviewer(uuid4(), name="Fed Back"), _make_interviewer(uuid4(), name="Pending")
    schedule = _make_schedule(status=InterviewStatus.SCHEDULED, start_at=past_start)
    service, cc_repo, campaign_repo, repo, audit_service, campaign_candidate = _make_env(
        schedule=schedule, interviewers=[fed_back, pending],
    )
    service.interview_feedback_repo.get_by_interview_schedule_id.return_value = [
        SimpleNamespace(interviewer_id=fed_back.id),
    ]

    with patch(
        "app.services.notifications.interview_feedback_request_emails.queue_interview_feedback_requested_email",
        return_value=True,
    ) as mock_queue_leaf:
        result = service.request_feedback(schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert result.queued_count == 1
    mock_queue_leaf.assert_called_once()
    assert mock_queue_leaf.call_args.args[3] is pending


# ----------------------------------------------------------------------
# Epic 5 follow-up, Part B - reschedule() blocks a COMPLETED round with
# a distinct, clear message (not the generic "must be SCHEDULED/
# RESCHEDULED/CANCELLED first" wording, since COMPLETED is terminal, not
# just "the wrong one of those 3").
# ----------------------------------------------------------------------

def test_reschedule_rejects_a_completed_round_with_a_distinct_message():
    schedule = _make_schedule(status=InterviewStatus.COMPLETED)
    service, *_rest = _make_env(schedule=schedule)

    with pytest.raises(CampaignException) as exc_info:
        service.reschedule(
            schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
            request=RescheduleInterviewRequest(
                interviewers=[InterviewerInput(name="Alice", email="alice@example.com")],
                date=date(2026, 8, 21), start_time=time(15, 0), end_time=time(16, 0), timezone="UTC",
            ),
        )

    assert exc_info.value.status_code == 409
    assert str(exc_info.value) == "This interview is marked complete and can no longer be edited."


# ----------------------------------------------------------------------
# Epic 5 follow-up, Part C - complete(), the "Mark as Completed" action.
# Reuses queue_pending_feedback_requests_for_round exactly like
# request_feedback() does - its own dedup/who's-pending behavior is
# already covered by that method's tests and
# test_interview_feedback_request_emails.py, so these focus on
# complete()'s own validation and its delegation to that shared function.
# ----------------------------------------------------------------------

def test_complete_rejects_a_round_whose_end_at_is_still_in_the_future():
    future_end = datetime.now(timezone.utc) + timedelta(hours=1)
    schedule = _make_schedule(status=InterviewStatus.SCHEDULED, end_at=future_end)
    service, *_rest = _make_env(schedule=schedule)

    with patch("app.services.interview_schedule_service.queue_pending_feedback_requests_for_round") as mock_queue:
        with pytest.raises(CampaignException) as exc_info:
            service.complete(schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 400
    assert str(exc_info.value) == "Cannot mark an interview complete before it has ended."
    mock_queue.assert_not_called()


def test_complete_rejects_a_round_with_no_end_at_at_all():
    schedule = _make_schedule(status=InterviewStatus.PENDING, end_at=None)
    service, *_rest = _make_env(schedule=schedule)

    with pytest.raises(CampaignException) as exc_info:
        service.complete(schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 400


def test_complete_rejects_an_already_completed_round_not_silently():
    schedule = _make_schedule(status=InterviewStatus.COMPLETED)
    service, *_rest = _make_env(schedule=schedule)

    with patch("app.services.interview_schedule_service.queue_pending_feedback_requests_for_round") as mock_queue:
        with pytest.raises(CampaignException) as exc_info:
            service.complete(schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 400
    assert str(exc_info.value) == "Cannot mark an interview complete - it is already COMPLETED."
    mock_queue.assert_not_called()


def test_complete_rejects_an_already_cancelled_round_not_silently():
    schedule = _make_schedule(status=InterviewStatus.CANCELLED)
    service, *_rest = _make_env(schedule=schedule)

    with pytest.raises(CampaignException) as exc_info:
        service.complete(schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 400
    assert str(exc_info.value) == "Cannot mark an interview complete - it is already CANCELLED."


def test_complete_flips_status_and_commits_before_queueing_feedback():
    past_end = datetime.now(timezone.utc) - timedelta(minutes=5)
    interviewer_a, interviewer_b = _make_interviewer(uuid4()), _make_interviewer(uuid4())
    schedule = _make_schedule(status=InterviewStatus.SCHEDULED, end_at=past_end)
    service, cc_repo, campaign_repo, repo, audit_service, campaign_candidate = _make_env(
        schedule=schedule, interviewers=[interviewer_a, interviewer_b],
    )

    with patch(
        "app.services.interview_schedule_service.queue_pending_feedback_requests_for_round", return_value=2,
    ) as mock_queue:
        result = service.complete(schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert schedule.status == InterviewStatus.COMPLETED
    repo.update.assert_called_once_with(schedule)
    repo.commit.assert_called_once()
    assert result.status == "COMPLETED"
    assert result.feedback_queued_count == 2
    mock_queue.assert_called_once_with(
        cc_repo.db, campaign_candidate, schedule, [interviewer_a, interviewer_b], service.interview_feedback_repo,
    )


def test_complete_returns_zero_feedback_queued_when_nothing_is_left_to_request():
    past_end = datetime.now(timezone.utc) - timedelta(minutes=5)
    schedule = _make_schedule(status=InterviewStatus.RESCHEDULED, end_at=past_end)
    service, *_rest = _make_env(schedule=schedule, interviewers=[_make_interviewer(uuid4())])

    with patch(
        "app.services.interview_schedule_service.queue_pending_feedback_requests_for_round", return_value=0,
    ):
        result = service.complete(schedule.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert result.status == "COMPLETED"
    assert result.feedback_queued_count == 0


def test_complete_rejects_a_non_owning_hiring_manager():
    past_end = datetime.now(timezone.utc) - timedelta(minutes=5)
    schedule = _make_schedule(status=InterviewStatus.SCHEDULED, end_at=past_end)
    service, *_rest = _make_env(schedule=schedule)

    with patch("app.services.interview_schedule_service.queue_pending_feedback_requests_for_round") as mock_queue:
        with pytest.raises(CampaignException) as exc_info:
            service.complete(schedule.id, actor_id="not-the-hm", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 403
    mock_queue.assert_not_called()


def test_complete_allows_hr_admin_regardless_of_ownership():
    past_end = datetime.now(timezone.utc) - timedelta(minutes=5)
    schedule = _make_schedule(status=InterviewStatus.SCHEDULED, end_at=past_end)
    service, *_rest = _make_env(schedule=schedule)

    with patch("app.services.interview_schedule_service.queue_pending_feedback_requests_for_round", return_value=0):
        result = service.complete(schedule.id, actor_id="anyone", actor_roles=["HR_ADMIN"])

    assert result.status == "COMPLETED"


def test_complete_raises_404_when_interview_not_found():
    service, *_rest = _make_env(schedule=None)
    service.interview_schedule_repo.get_by_id.return_value = None

    with pytest.raises(CampaignException) as exc_info:
        service.complete(uuid4(), actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 404


# ----------------------------------------------------------------------
# Campaign-wide interview calendar follow-up - get_campaign_interviews /
# _authorize_campaign_access. A different fixture shape from _make_env
# (which is candidate-scoped) since this is the first campaign-scoped
# (not campaign-candidate-scoped) read on this service.
# ----------------------------------------------------------------------

def _make_candidate_row(campaign_candidate_id):
    campaign_candidate = SimpleNamespace(id=campaign_candidate_id, campaign_id=uuid4())
    candidate = SimpleNamespace(id=uuid4(), full_name_encrypted=b"enc(Jordan Lee)", encryption_key_id=uuid4())
    return (campaign_candidate, candidate, None)


def _campaign_interview_env(campaign=None, schedules=None, interviewers=None, campaign_rows=None):
    campaign = campaign or _make_campaign(hiring_manager_id="hm-1")
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_all_by_campaign.return_value = campaign_rows or []

    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign

    interview_schedule_repo = MagicMock()
    interview_schedule_repo.get_by_campaign_id.return_value = schedules or []
    interview_schedule_repo.get_active_interviewers_by_interview_ids.return_value = interviewers or []

    service = InterviewScheduleService(
        interview_schedule_repo, campaign_candidate_repo, campaign_repo, MagicMock(),
        MagicMock(), MagicMock(), MagicMock(),
    )
    return service, campaign, campaign_candidate_repo, campaign_repo, interview_schedule_repo


def test_get_campaign_interviews_raises_404_when_campaign_not_found():
    service, *_rest = _campaign_interview_env()
    service.campaign_repo.get_by_id.return_value = None

    with pytest.raises(CampaignException) as exc_info:
        service.get_campaign_interviews(uuid4(), actor_id="hr-1", actor_roles=["HR_ADMIN"])

    assert exc_info.value.status_code == 404


def test_get_campaign_interviews_hr_admin_bypasses_ownership():
    campaign = _make_campaign(hiring_manager_id="someone-else")
    service, campaign, *_rest = _campaign_interview_env(campaign=campaign)

    result = service.get_campaign_interviews(campaign.id, actor_id="hr-1", actor_roles=["HR_ADMIN"])

    assert result == []


def test_get_campaign_interviews_owning_hiring_manager_succeeds():
    campaign = _make_campaign(hiring_manager_id="hm-1")
    service, campaign, *_rest = _campaign_interview_env(campaign=campaign)

    result = service.get_campaign_interviews(campaign.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert result == []


def test_get_campaign_interviews_non_owning_hiring_manager_raises_403():
    campaign = _make_campaign(hiring_manager_id="someone-else")
    service, campaign, *_rest = _campaign_interview_env(campaign=campaign)

    with pytest.raises(CampaignException) as exc_info:
        service.get_campaign_interviews(campaign.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 403


def test_get_campaign_interviews_recruiter_with_access_succeeds():
    campaign = _make_campaign(hiring_manager_id="someone-else")
    service, campaign, *_rest = _campaign_interview_env(campaign=campaign)

    with patch("app.services.interview_schedule_service.DashboardRepository") as mock_dashboard_repo_cls:
        mock_dashboard_repo_cls.return_value.is_campaign_accessible_to_recruiter.return_value = True
        result = service.get_campaign_interviews(campaign.id, actor_id="recruiter-1", actor_roles=["RECRUITER"])

    assert result == []
    mock_dashboard_repo_cls.return_value.is_campaign_accessible_to_recruiter.assert_called_once_with(
        "recruiter-1", campaign.id,
    )


def test_get_campaign_interviews_recruiter_without_access_raises_403():
    campaign = _make_campaign(hiring_manager_id="someone-else")
    service, campaign, *_rest = _campaign_interview_env(campaign=campaign)

    with patch("app.services.interview_schedule_service.DashboardRepository") as mock_dashboard_repo_cls:
        mock_dashboard_repo_cls.return_value.is_campaign_accessible_to_recruiter.return_value = False
        with pytest.raises(CampaignException) as exc_info:
            service.get_campaign_interviews(campaign.id, actor_id="recruiter-1", actor_roles=["RECRUITER"])

    assert exc_info.value.status_code == 403


def test_get_campaign_interviews_builds_entries_with_candidate_name_and_active_interviewers_only():
    campaign = _make_campaign(hiring_manager_id="hm-1")
    schedule = _make_schedule(status=InterviewStatus.SCHEDULED, campaign_candidate_id=uuid4())
    interviewer = _make_interviewer(schedule.id, name="Alice", email="alice@example.com")
    campaign_row = _make_candidate_row(schedule.campaign_candidate_id)
    service, campaign, *_rest = _campaign_interview_env(
        campaign=campaign, schedules=[schedule], interviewers=[interviewer], campaign_rows=[campaign_row],
    )

    with patch("app.services.interview_schedule_service.EncryptionService") as mock_encryption_cls:
        mock_encryption_cls.return_value.decrypt.return_value = "Jordan Lee"
        result = service.get_campaign_interviews(campaign.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert len(result) == 1
    entry = result[0]
    assert entry.id == schedule.id
    assert entry.campaign_candidate_id == schedule.campaign_candidate_id
    assert entry.candidate_name == "Jordan Lee"
    assert entry.status == "SCHEDULED"
    assert entry.timezone == "UTC"
    assert len(entry.interviewers) == 1
    assert entry.interviewers[0].email == "alice@example.com"


def test_get_campaign_interviews_falls_back_to_unknown_when_candidate_row_missing():
    campaign = _make_campaign(hiring_manager_id="hm-1")
    schedule = _make_schedule(status=InterviewStatus.SCHEDULED, campaign_candidate_id=uuid4())
    service, campaign, *_rest = _campaign_interview_env(
        campaign=campaign, schedules=[schedule], campaign_rows=[],
    )

    result = service.get_campaign_interviews(campaign.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert result[0].candidate_name == "Unknown"


def test_get_campaign_interviews_passes_filters_through_to_the_repository():
    campaign = _make_campaign(hiring_manager_id="hm-1")
    service, campaign, _cc_repo, _c_repo, interview_schedule_repo = _campaign_interview_env(campaign=campaign)

    service.get_campaign_interviews(
        campaign.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
        statuses=[InterviewStatus.SCHEDULED], interviewer_email="alice@example.com",
    )

    interview_schedule_repo.get_by_campaign_id.assert_called_once()
    call_kwargs = interview_schedule_repo.get_by_campaign_id.call_args.kwargs
    assert call_kwargs["start_at_after"] == datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert call_kwargs["start_at_before"] == datetime(2026, 8, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
    assert call_kwargs["statuses"] == [InterviewStatus.SCHEDULED]
    assert call_kwargs["interviewer_email"] == "alice@example.com"


def test_get_campaign_interviews_no_filters_returns_everything_for_the_campaign():
    campaign = _make_campaign(hiring_manager_id="hm-1")
    schedule_1 = _make_schedule(status=InterviewStatus.SCHEDULED, campaign_candidate_id=uuid4())
    schedule_2 = _make_schedule(status=InterviewStatus.COMPLETED, campaign_candidate_id=uuid4())
    service, campaign, *_rest = _campaign_interview_env(campaign=campaign, schedules=[schedule_1, schedule_2])

    result = service.get_campaign_interviews(campaign.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert len(result) == 2
    assert {r.id for r in result} == {schedule_1.id, schedule_2.id}


def test_get_campaign_interviews_empty_result_skips_the_candidate_and_interviewer_lookups():
    campaign = _make_campaign(hiring_manager_id="hm-1")
    service, campaign, campaign_candidate_repo, _c_repo, interview_schedule_repo = _campaign_interview_env(
        campaign=campaign, schedules=[],
    )

    result = service.get_campaign_interviews(campaign.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert result == []
    campaign_candidate_repo.get_all_by_campaign.assert_not_called()
    interview_schedule_repo.get_active_interviewers_by_interview_ids.assert_not_called()
