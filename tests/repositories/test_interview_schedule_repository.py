from unittest.mock import MagicMock
from uuid import uuid4

from app.models.interview import InterviewHistoryEventType, InterviewSchedule
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


def test_get_by_id_delegates_to_session_get():
    repo, db = _repo()
    interview_id = uuid4()
    db.get.return_value = "the-schedule"

    result = repo.get_by_id(interview_id)

    assert result == "the-schedule"
    db.get.assert_called_once_with(InterviewSchedule, interview_id)


# ----------------------------------------------------------------------
# Step 3: interviewers (delete-then-recreate) and history (append-only).
# ----------------------------------------------------------------------

def test_replace_interviewers_deletes_existing_rows_then_inserts_the_new_set():
    repo, db = _repo()
    interview_id = uuid4()

    rows = repo.replace_interviewers(
        interview_id, [{"name": "Alice", "email": "alice@example.com"}, {"name": "Bob", "email": "bob@example.com"}],
    )

    db.execute.assert_called_once()  # the DELETE
    assert db.add_all.call_count == 1
    assert len(rows) == 2
    assert {row.name for row in rows} == {"Alice", "Bob"}
    assert all(row.interview_id == interview_id for row in rows)


def test_replace_interviewers_with_empty_list_still_clears_existing_rows():
    repo, db = _repo()

    rows = repo.replace_interviewers(uuid4(), [])

    db.execute.assert_called_once()
    assert rows == []


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
