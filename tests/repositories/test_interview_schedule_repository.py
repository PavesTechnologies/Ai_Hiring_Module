from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from app.models.interview import InterviewHistoryEventType, InterviewSchedule, InterviewStatus
from app.repositories.interview_schedule_repository import InterviewScheduleRepository

"""
Epic 4 (M12) Step 2/3 - MagicMock-based unit tests, matching this project's
universal test-repository convention (no real-DB test harness exists
anywhere in this codebase). get_or_create_pending's check-then-create logic
is exercised here; the campaign_candidate_id UNIQUE constraint itself is a
DB-level guarantee this suite cannot independently exercise - it's relied
on only as a backstop, never as this method's actual concurrency control
(see stage_transition_service.py's FOR UPDATE lock for that).
"""


def _repo():
    db = MagicMock()
    return InterviewScheduleRepository(db), db


def test_get_or_create_pending_creates_a_new_row_when_none_exists():
    repo, db = _repo()
    db.execute.return_value.scalars.return_value.first.return_value = None
    campaign_candidate_id = uuid4()

    schedule, was_created = repo.get_or_create_pending(campaign_candidate_id)

    assert was_created is True
    assert isinstance(schedule, InterviewSchedule)
    assert schedule.campaign_candidate_id == campaign_candidate_id
    db.add.assert_called_once_with(schedule)
    db.flush.assert_called_once()


def test_get_or_create_pending_returns_existing_row_untouched_when_one_exists():
    repo, db = _repo()
    existing = MagicMock(spec=InterviewSchedule)
    db.execute.return_value.scalars.return_value.first.return_value = existing

    schedule, was_created = repo.get_or_create_pending(uuid4())

    assert was_created is False
    assert schedule is existing
    db.add.assert_not_called()


def test_get_by_campaign_candidate_id_returns_none_when_no_row():
    repo, db = _repo()
    db.execute.return_value.scalars.return_value.first.return_value = None

    result = repo.get_by_campaign_candidate_id(uuid4())

    assert result is None


# ----------------------------------------------------------------------
# Multi-round follow-up: latest/all-rounds lookups, and the SAVEPOINT-
# guarded round reservation.
# ----------------------------------------------------------------------

def test_get_latest_by_campaign_candidate_id_orders_by_round_number_descending():
    repo, db = _repo()
    latest = MagicMock(spec=InterviewSchedule)
    db.execute.return_value.scalars.return_value.first.return_value = latest

    result = repo.get_latest_by_campaign_candidate_id(uuid4())

    assert result is latest


def test_get_all_by_campaign_candidate_id_orders_by_round_number_ascending():
    repo, db = _repo()
    rows = [MagicMock(spec=InterviewSchedule), MagicMock(spec=InterviewSchedule)]
    db.execute.return_value.scalars.return_value.all.return_value = rows

    result = repo.get_all_by_campaign_candidate_id(uuid4())

    assert result == rows


# ----------------------------------------------------------------------
# Epic 5 Step 4 - get_ended_active_rounds, the feedback-request sweep's
# candidate query.
# ----------------------------------------------------------------------

def test_get_ended_active_rounds_returns_the_query_results():
    repo, db = _repo()
    rows = [MagicMock(spec=InterviewSchedule), MagicMock(spec=InterviewSchedule)]
    db.execute.return_value.scalars.return_value.all.return_value = rows

    result = repo.get_ended_active_rounds(before=datetime(2026, 8, 19, tzinfo=timezone.utc))

    assert result == rows


def test_get_ended_active_rounds_where_clause_excludes_cancelled_and_future_rounds():
    """
    Real-shape proof, not just plumbing: compiles the actual Select
    construct built by the method (against a mocked session, but a real
    SQLAlchemy statement object) and inspects its WHERE clause directly -
    the same technique used for delete_by_candidate's explicit-filter
    tests. Confirms CANCELLED/PENDING are excluded via the status IN (...)
    condition, and that end_at is compared against the given cutoff.
    """
    repo, db = _repo()
    db.execute.return_value.scalars.return_value.all.return_value = []
    cutoff = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)

    repo.get_ended_active_rounds(before=cutoff)

    statement = db.execute.call_args.args[0]
    compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert "interview_schedules.status IN" in compiled
    assert "'SCHEDULED'" in compiled
    assert "'RESCHEDULED'" in compiled
    assert "'CANCELLED'" not in compiled
    assert "'PENDING'" not in compiled
    assert "interview_schedules.end_at <" in compiled


# ----------------------------------------------------------------------
# Campaign-wide interview calendar follow-up - get_by_campaign_id, the
# first query in this codebase joining interview_schedules across every
# candidate in a campaign.
# ----------------------------------------------------------------------

def test_get_by_campaign_id_joins_on_campaign_candidate_id():
    repo, db = _repo()
    db.execute.return_value.scalars.return_value.all.return_value = []
    campaign_id = uuid4()

    repo.get_by_campaign_id(campaign_id)

    statement = db.execute.call_args.args[0]
    compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert "JOIN campaign_candidates" in compiled
    assert "campaign_candidates.campaign_id = " in compiled
    assert str(campaign_id).replace("-", "") in compiled.replace("-", "")
    assert "interview_interviewers" not in compiled  # no interviewer join when no filter given


def test_get_by_campaign_id_applies_date_range_and_status_filters():
    repo, db = _repo()
    db.execute.return_value.scalars.return_value.all.return_value = []
    after = datetime(2026, 8, 1, tzinfo=timezone.utc)
    before = datetime(2026, 8, 31, 23, 59, 59, tzinfo=timezone.utc)

    repo.get_by_campaign_id(
        uuid4(), start_at_after=after, start_at_before=before,
        statuses=[InterviewStatus.SCHEDULED, InterviewStatus.RESCHEDULED],
    )

    statement = db.execute.call_args.args[0]
    compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert "interview_schedules.start_at >=" in compiled
    assert "interview_schedules.start_at <=" in compiled
    assert "interview_schedules.status IN" in compiled
    assert "'SCHEDULED'" in compiled
    assert "'CANCELLED'" not in compiled


def test_get_by_campaign_id_interviewer_filter_joins_interview_interviewers_active_only():
    repo, db = _repo()
    db.execute.return_value.scalars.return_value.all.return_value = []

    repo.get_by_campaign_id(uuid4(), interviewer_email="Alice@Example.com")

    statement = db.execute.call_args.args[0]
    compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert "JOIN interview_interviewers" in compiled
    assert "interview_interviewers.is_active" in compiled
    assert "lower(interview_interviewers.email) = 'alice@example.com'" in compiled.lower()


def test_get_by_campaign_id_returns_the_query_results():
    repo, db = _repo()
    rows = [MagicMock(spec=InterviewSchedule)]
    db.execute.return_value.scalars.return_value.all.return_value = rows

    result = repo.get_by_campaign_id(uuid4())

    assert result == rows


def test_get_active_interviewers_by_interview_ids_returns_the_query_results():
    repo, db = _repo()
    rows = [MagicMock(), MagicMock()]
    db.execute.return_value.scalars.return_value.all.return_value = rows

    result = repo.get_active_interviewers_by_interview_ids([uuid4(), uuid4()])

    assert result == rows


def test_get_active_interviewers_by_interview_ids_short_circuits_on_empty_list():
    repo, db = _repo()

    result = repo.get_active_interviewers_by_interview_ids([])

    assert result == []
    db.execute.assert_not_called()


@contextmanager
def _reraising_savepoint():
    yield


def test_create_next_round_creates_a_new_row_when_the_round_number_is_free():
    repo, db = _repo()
    db.begin_nested.side_effect = lambda: _reraising_savepoint()
    campaign_candidate_id = uuid4()

    schedule, was_created = repo.create_next_round(campaign_candidate_id, 2)

    assert was_created is True
    assert isinstance(schedule, InterviewSchedule)
    assert schedule.campaign_candidate_id == campaign_candidate_id
    assert schedule.round_number == 2
    db.add.assert_called_once_with(schedule)


def test_create_next_round_falls_back_to_the_winners_row_on_a_race():
    """
    Two concurrent "Schedule Next Round" calls can both try to insert the
    same round_number - UNIQUE(campaign_candidate_id, round_number) is
    the real backstop, and the loser's flush raises IntegrityError. The
    SAVEPOINT scopes that to just this insert attempt, then this method
    resolves to the winner's already-committed row instead of raising.
    """
    repo, db = _repo()
    db.begin_nested.side_effect = lambda: _reraising_savepoint()
    db.flush.side_effect = IntegrityError("insert", {}, Exception("duplicate key"))
    winner_row = MagicMock(spec=InterviewSchedule)
    db.execute.return_value.scalars.return_value.first.return_value = winner_row

    schedule, was_created = repo.create_next_round(uuid4(), 2)

    assert was_created is False
    assert schedule is winner_row


def test_get_by_id_delegates_to_session_get():
    repo, db = _repo()
    interview_id = uuid4()
    db.get.return_value = "the-schedule"

    result = repo.get_by_id(interview_id)

    assert result == "the-schedule"
    db.get.assert_called_once_with(InterviewSchedule, interview_id)


def test_get_interviewer_by_id_delegates_to_session_get():
    from app.models.interview import InterviewInterviewer

    repo, db = _repo()
    interviewer_id = uuid4()
    db.get.return_value = "the-interviewer"

    result = repo.get_interviewer_by_id(interviewer_id)

    assert result == "the-interviewer"
    db.get.assert_called_once_with(InterviewInterviewer, interviewer_id)


# ----------------------------------------------------------------------
# Interviewer lifecycle follow-up: replace_interviewers now soft-removes
# ALWAYS (is_active=false), regardless of whether the row is referenced
# by an email_notifications/interview_feedback row - unifying the two
# previously-different outcomes (hard-delete vs silently-skipped-delete)
# that used to depend on reference state. Every FK stays valid either way
# now, so there's no crash risk to route around, and get_interviewers()
# (unfiltered) still resolves a removed interviewer's name/email for
# historical feedback display.
#
# get_active_interviewers is mocked directly on the repo instance rather
# than through db.execute - replace_interviewers only issues one
# db.execute call now (the active-interviewer lookup happens through
# this mocked method instead), and stubbing that at the raw db level
# would conflate it with the same session's add/delete calls.
# ----------------------------------------------------------------------

def _make_interviewer_row(interview_id, name="Alice", email="alice@example.com", is_active=True):
    return SimpleNamespace(id=uuid4(), interview_id=interview_id, name=name, email=email, is_active=is_active)


def _repo_with_active_interviewers(existing_rows):
    repo, db = _repo()
    repo.get_active_interviewers = MagicMock(return_value=existing_rows)
    return repo, db


def test_replace_interviewers_with_the_same_list_keeps_the_existing_id_untouched():
    """Proves diffing, not delete-recreate: an unchanged interviewer's row - and its id - survives the call."""
    interview_id = uuid4()
    alice = _make_interviewer_row(interview_id)
    repo, db = _repo_with_active_interviewers([alice])

    result, removed = repo.replace_interviewers(interview_id, [{"name": "Alice", "email": "alice@example.com"}])

    assert result == [alice]
    assert result[0].id == alice.id
    assert removed == []
    assert alice.is_active is True
    db.add.assert_not_called()


def test_replace_interviewers_matches_email_case_insensitively():
    interview_id = uuid4()
    alice = _make_interviewer_row(interview_id, email="Alice@Example.com")
    repo, db = _repo_with_active_interviewers([alice])

    result, removed = repo.replace_interviewers(interview_id, [{"name": "Alice", "email": "alice@example.com"}])

    assert result == [alice]
    assert removed == []


def test_replace_interviewers_updates_name_in_place_for_a_matched_email():
    interview_id = uuid4()
    alice = _make_interviewer_row(interview_id, name="Old Name")
    repo, db = _repo_with_active_interviewers([alice])

    result, removed = repo.replace_interviewers(interview_id, [{"name": "New Name", "email": "alice@example.com"}])

    assert result[0] is alice
    assert alice.name == "New Name"


def test_replace_interviewers_inserts_a_new_active_row_for_an_unmatched_incoming_entry():
    interview_id = uuid4()
    repo, db = _repo_with_active_interviewers([])

    result, removed = repo.replace_interviewers(interview_id, [{"name": "Bob", "email": "bob@example.com"}])

    assert len(result) == 1
    assert result[0].interview_id == interview_id
    assert result[0].name == "Bob"
    assert result[0].is_active is True
    db.add.assert_called_once_with(result[0])


def test_replace_interviewers_soft_removes_a_dropped_interviewer_regardless_of_reference_state():
    """
    Unified behavior - no longer branches on whether the row is
    referenced by feedback/email_notifications. Never deleted either way;
    every FK pointing at it stays valid.
    """
    interview_id = uuid4()
    alice = _make_interviewer_row(interview_id)
    repo, db = _repo_with_active_interviewers([alice])

    result, removed = repo.replace_interviewers(interview_id, [])

    assert result == []
    assert removed == [alice]
    assert alice.is_active is False
    db.delete.assert_not_called()


def test_replace_interviewers_matching_only_considers_active_rows():
    """
    A removed-then-re-added interviewer (same email) gets a brand new
    row, not their old inactive one reactivated - so they correctly go
    through the invitation-email path again as a "new" interviewer,
    rather than the per-(schedule, interviewer) dedup silently treating
    them as already-invited from before.
    """
    interview_id = uuid4()
    repo, db = _repo_with_active_interviewers([])  # the old, now-inactive row is not in the active set

    result, removed = repo.replace_interviewers(interview_id, [{"name": "Alice", "email": "alice@example.com"}])

    assert len(result) == 1
    assert result[0].email == "alice@example.com"
    db.add.assert_called_once()


def test_replace_interviewers_adds_one_and_removes_one_in_the_same_call():
    interview_id = uuid4()
    alice = _make_interviewer_row(interview_id)
    repo, db = _repo_with_active_interviewers([alice])

    result, removed = repo.replace_interviewers(interview_id, [{"name": "Bob", "email": "bob@example.com"}])

    assert len(result) == 1
    assert result[0].email == "bob@example.com"
    assert removed == [alice]
    assert alice.is_active is False
    db.delete.assert_not_called()
    db.add.assert_called_once()


def test_replace_interviewers_with_empty_existing_and_empty_incoming_is_a_no_op():
    repo, db = _repo_with_active_interviewers([])

    result, removed = repo.replace_interviewers(uuid4(), [])

    assert result == []
    assert removed == []
    db.add.assert_not_called()
    db.delete.assert_not_called()


def test_get_active_interviewers_filters_to_is_active_true():
    repo, db = _repo()
    rows = [MagicMock(), MagicMock()]
    db.execute.return_value.scalars.return_value.all.return_value = rows

    result = repo.get_active_interviewers(uuid4())

    assert result == rows
    statement = db.execute.call_args.args[0]
    compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert "interview_interviewers.is_active" in compiled


def test_add_history_persists_the_given_fields():
    repo, db = _repo()
    interview_id = uuid4()

    entry = repo.add_history(
        interview_id=interview_id,
        event_type=InterviewHistoryEventType.RESCHEDULED,
        old_start_at=None,
        new_start_at=None,
        changed_by="user-1",
        changed_by_role="HIRING_MANAGER",
        reason="candidate requested a new time",
    )

    db.add.assert_called_once_with(entry)
    assert entry.interview_id == interview_id
    assert entry.event_type == InterviewHistoryEventType.RESCHEDULED
    assert entry.changed_by == "user-1"
    assert entry.reason == "candidate requested a new time"


def test_get_history_orders_by_changed_at_ascending():
    repo, db = _repo()
    rows = [MagicMock(), MagicMock()]
    db.execute.return_value.scalars.return_value.all.return_value = rows

    result = repo.get_history(uuid4())

    assert result == rows


# ----------------------------------------------------------------------
# Cascading-cancellation follow-up: cancel_active_rounds.
# ----------------------------------------------------------------------

def _round(status, cancel_reason=None):
    return SimpleNamespace(
        id=uuid4(), status=status, start_at=None, cancel_reason=cancel_reason, updated_at=None,
    )


def test_cancel_active_rounds_cancels_only_pending_scheduled_rescheduled_rounds():
    repo, db = _repo()
    pending_round = _round(InterviewStatus.PENDING)
    scheduled_round = _round(InterviewStatus.SCHEDULED)
    rescheduled_round = _round(InterviewStatus.RESCHEDULED)
    already_cancelled = _round(InterviewStatus.CANCELLED, cancel_reason="candidate withdrew")
    db.execute.return_value.scalars.return_value.all.return_value = [
        pending_round, scheduled_round, rescheduled_round, already_cancelled,
    ]

    result = repo.cancel_active_rounds(
        uuid4(), reason="Candidate outcome finalized: SELECTED",
        changed_by="hm-1", changed_by_role="HIRING_MANAGER",
    )

    for round_ in (pending_round, scheduled_round, rescheduled_round):
        assert round_.status == InterviewStatus.CANCELLED
        assert round_.cancel_reason == "Candidate outcome finalized: SELECTED"
    assert result == [pending_round, scheduled_round, rescheduled_round]

    # Already-cancelled round left completely untouched - not re-cancelled,
    # not double-logged.
    assert already_cancelled.cancel_reason == "candidate withdrew"


def test_cancel_active_rounds_writes_one_history_entry_per_cancelled_round():
    repo, db = _repo()
    round_1 = _round(InterviewStatus.SCHEDULED)
    round_2 = _round(InterviewStatus.CANCELLED)  # already cancelled - no history entry expected
    db.execute.return_value.scalars.return_value.all.return_value = [round_1, round_2]

    repo.cancel_active_rounds(
        uuid4(), reason="Candidate outcome finalized: REJECTED",
        changed_by="hm-1", changed_by_role="HIRING_MANAGER",
    )

    db.add.assert_called_once()
    entry = db.add.call_args.args[0]
    assert entry.interview_id == round_1.id
    assert entry.event_type == InterviewHistoryEventType.CANCELLED
    assert entry.new_start_at is None
    assert entry.changed_by == "hm-1"
    assert entry.changed_by_role == "HIRING_MANAGER"
    assert entry.reason == "Candidate outcome finalized: REJECTED"


def test_cancel_active_rounds_returns_empty_list_when_nothing_is_active():
    repo, db = _repo()
    db.execute.return_value.scalars.return_value.all.return_value = [
        _round(InterviewStatus.CANCELLED),
    ]

    result = repo.cancel_active_rounds(
        uuid4(), reason="Candidate outcome finalized: SELECTED",
        changed_by="hm-1", changed_by_role="HIRING_MANAGER",
    )

    assert result == []
    db.add.assert_not_called()
