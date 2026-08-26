from contextlib import contextmanager
from unittest.mock import MagicMock
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from app.models.interview import InterviewFeedback, InterviewFeedbackRecommendation
from app.repositories.interview_feedback_repository import InterviewFeedbackRepository


def _repo():
    db = MagicMock()
    return InterviewFeedbackRepository(db), db


@contextmanager
def _reraising_savepoint():
    yield


def test_create_inserts_a_new_row_when_none_exists():
    repo, db = _repo()
    db.begin_nested.side_effect = lambda: _reraising_savepoint()
    interview_schedule_id, interviewer_id = uuid4(), uuid4()

    feedback, was_created = repo.create(
        interview_schedule_id, interviewer_id, InterviewFeedbackRecommendation.ADVANCE, "Strong candidate",
    )

    assert was_created is True
    assert isinstance(feedback, InterviewFeedback)
    assert feedback.interview_schedule_id == interview_schedule_id
    assert feedback.interviewer_id == interviewer_id
    assert feedback.recommendation == InterviewFeedbackRecommendation.ADVANCE
    assert feedback.notes == "Strong candidate"
    db.add.assert_called_once_with(feedback)


def test_create_returns_the_existing_row_on_a_duplicate_submission_not_a_raw_error():
    """
    UNIQUE(interview_schedule_id, interviewer_id) is a hard lock - this
    method resolves the race cleanly (same SAVEPOINT shape as
    create_idempotent elsewhere), but does NOT treat it as a success the
    way get_or_create_pending does. was_created=False is the signal the
    service layer turns into a 409, not a silent "here's what's there."
    """
    repo, db = _repo()
    db.begin_nested.side_effect = lambda: _reraising_savepoint()
    db.flush.side_effect = IntegrityError("insert", {}, Exception("duplicate key"))
    existing_row = MagicMock(spec=InterviewFeedback)
    db.execute.return_value.scalars.return_value.first.return_value = existing_row

    feedback, was_created = repo.create(uuid4(), uuid4(), InterviewFeedbackRecommendation.SELECT, None)

    assert was_created is False
    assert feedback is existing_row


def test_get_by_interview_schedule_id_orders_by_submitted_at_ascending():
    repo, db = _repo()
    rows = [MagicMock(), MagicMock()]
    db.execute.return_value.scalars.return_value.all.return_value = rows

    result = repo.get_by_interview_schedule_id(uuid4())

    assert result == rows


def test_get_by_interview_schedule_id_and_interviewer_id_returns_the_row():
    repo, db = _repo()
    row = MagicMock(spec=InterviewFeedback)
    db.execute.return_value.scalars.return_value.first.return_value = row

    result = repo.get_by_interview_schedule_id_and_interviewer_id(uuid4(), uuid4())

    assert result is row


def test_get_by_interview_schedule_id_and_interviewer_id_returns_none_when_no_row():
    repo, db = _repo()
    db.execute.return_value.scalars.return_value.first.return_value = None

    result = repo.get_by_interview_schedule_id_and_interviewer_id(uuid4(), uuid4())

    assert result is None
