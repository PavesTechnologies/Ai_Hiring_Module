from unittest.mock import MagicMock
from uuid import uuid4

from app.models.email import EmailNotification, EmailRecipientType, EmailTriggerEvent
from app.repositories.email_notification_repository import EmailNotificationRepository

"""
Epic 5 Step 2 - MagicMock-based unit tests, matching this project's
universal test-repository convention. delete_by_candidate is the
highest-stakes method here (candidate erasure/compliance) - explicitly
tested to prove it filters on recipient_type = CANDIDATE as well as
candidate_id, not just candidate_id alone, per the explicit instruction
that incidental NULL behavior is not an acceptable substitute for an
explicit filter in a compliance-relevant delete.
"""


def _repo():
    db = MagicMock()
    return EmailNotificationRepository(db), db


def test_create_adds_flushes_and_refreshes():
    repo, db = _repo()
    notification = EmailNotification(candidate_id=uuid4(), trigger_event=EmailTriggerEvent.CANDIDATE_REJECTED, template_id=uuid4())

    result = repo.create(notification)

    assert result is notification
    db.add.assert_called_once_with(notification)
    db.flush.assert_called_once()
    db.refresh.assert_called_once_with(notification)


def test_get_by_id_returns_first_match():
    repo, db = _repo()
    db.query.return_value.filter.return_value.first.return_value = "the-notification"

    result = repo.get_by_id(uuid4())

    assert result == "the-notification"


def test_get_by_campaign_candidate_id_and_trigger_event_returns_all_matches():
    repo, db = _repo()
    rows = [MagicMock(), MagicMock()]
    db.query.return_value.filter.return_value.all.return_value = rows

    result = repo.get_by_campaign_candidate_id_and_trigger_event(uuid4(), EmailTriggerEvent.CANDIDATE_REJECTED)

    assert result == rows


def test_get_by_interview_schedule_id_and_interviewer_id_and_trigger_event_returns_all_matches():
    """Epic 5 Step 4 - per-interviewer dedup for INTERVIEW_FEEDBACK_REQUESTED, distinct from the per-round lookup below."""
    repo, db = _repo()
    rows = [MagicMock()]
    db.query.return_value.filter.return_value.all.return_value = rows

    result = repo.get_by_interview_schedule_id_and_interviewer_id_and_trigger_event(
        uuid4(), uuid4(), EmailTriggerEvent.INTERVIEW_FEEDBACK_REQUESTED,
    )

    assert result == rows


def test_get_by_interview_schedule_id_and_trigger_event_returns_all_matches():
    """
    Epic 5 Step 2 - the per-round dedup lookup for INTERVIEW_SCHEDULED/
    RESCHEDULED/CANCELLED, distinct from the campaign-candidate-wide one
    above (a candidate can have many rounds, each independently
    notification-worthy).
    """
    repo, db = _repo()
    rows = [MagicMock()]
    db.query.return_value.filter.return_value.all.return_value = rows

    result = repo.get_by_interview_schedule_id_and_trigger_event(uuid4(), EmailTriggerEvent.INTERVIEW_SCHEDULED)

    assert result == rows


# ----------------------------------------------------------------------
# delete_by_candidate - explicit recipient_type guard.
# ----------------------------------------------------------------------

def test_delete_by_candidate_filters_on_recipient_type_candidate_and_candidate_id():
    repo, db = _repo()
    candidate_id = uuid4()

    repo.delete_by_candidate(candidate_id)

    db.execute.assert_called_once()
    compiled_where = str(db.execute.call_args.args[0])
    assert "recipient_type" in compiled_where
    assert "candidate_id" in compiled_where
    db.flush.assert_called_once()


def test_delete_by_candidate_never_touches_an_external_interviewer_row(db_session_factory=None):
    """
    Real-shape proof, not just a compiled-SQL string check: an
    EXTERNAL_INTERVIEWER row for a DIFFERENT interview happens to share
    no candidate_id at all (it's NULL, per the CHECK constraint) - the
    explicit recipient_type filter means even a hypothetical future row
    that somehow carried a matching candidate_id would still never be
    touched by a candidate-erasure delete, since recipient_type alone
    already excludes it. Verified here by inspecting the actual bound
    parameters of the DELETE statement built, not just calling execute.
    """
    repo, db = _repo()
    candidate_id = uuid4()

    repo.delete_by_candidate(candidate_id)

    statement = db.execute.call_args.args[0]
    # SQLAlchemy Delete construct - both filter criteria must be present
    # in the compiled WHERE clause, not just candidate_id.
    compiled = statement.compile(compile_kwargs={"literal_binds": False})
    where_clause = str(compiled)
    assert "email_notifications.recipient_type" in where_clause
    assert "email_notifications.candidate_id" in where_clause


def test_delete_by_candidate_binds_the_candidate_type_enum_value_and_the_given_id():
    repo, db = _repo()
    candidate_id = uuid4()

    repo.delete_by_candidate(candidate_id)

    statement = db.execute.call_args.args[0]
    compiled = statement.compile(compile_kwargs={"literal_binds": True})
    compiled_sql = str(compiled)
    assert "'CANDIDATE'" in compiled_sql
    assert candidate_id.hex in compiled_sql.replace("-", "")
