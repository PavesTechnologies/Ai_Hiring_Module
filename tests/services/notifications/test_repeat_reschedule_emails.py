"""
Review item 17: a second reschedule (or cancellation) of the same interview round is emailed too.
The dedup key carries the interview history event behind it, so only a repeat of the same event is
skipped - not the next one.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Query, Session

from app.models.email import EmailNotificationStatus, EmailTriggerEvent
from app.services.notifications.dedup import interview_event_id, notification_dedup_key


def _schedule():
    return SimpleNamespace(id=uuid4(), start_at=datetime(2026, 12, 1, 9, tzinfo=timezone.utc), timezone="UTC",
                           candidate_timezone=None, platform=None, meeting_link=None, location=None, notes=None)


def test_the_history_query_takes_the_latest_event_of_the_matching_type():
    captured = {}

    def first(self):
        captured["sql"] = str(self.statement.compile(dialect=postgresql.dialect())).lower()
        return SimpleNamespace(id=uuid4())

    with patch.object(Query, "first", first):
        event_id = interview_event_id(Session(), uuid4(), EmailTriggerEvent.INTERVIEW_RESCHEDULED)
    assert event_id is not None
    sql = captured["sql"]
    assert "interview_schedule_history.event_type =" in sql
    assert "order by interview_schedule_history.changed_at desc" in sql


def test_only_repeatable_triggers_look_up_an_event():
    db = MagicMock()
    assert interview_event_id(db, uuid4(), EmailTriggerEvent.INTERVIEW_SCHEDULED) is None
    assert interview_event_id(db, uuid4(), EmailTriggerEvent.INTERVIEW_INTERVIEWER_INVITATION) is None
    db.query.assert_not_called()


def _queue_candidate_reschedule(schedule, event_id, existing):
    from app.services.notifications import candidate_notification_emails as mod
    repo = MagicMock()
    repo.get_by_interview_schedule_id_and_trigger_event.return_value = existing
    repo.create_unless_duplicate.side_effect = lambda n: n
    with patch.object(mod, "interview_event_id", return_value=event_id), \
            patch.object(mod, "EmailNotificationRepository", return_value=repo), \
            patch.object(mod, "EmailTemplateRepository", return_value=MagicMock()), \
            patch.object(mod, "send_candidate_email_task") as task:
        mod.queue_interview_rescheduled_email(MagicMock(), SimpleNamespace(id=uuid4(), candidate_id=uuid4()),
                                              schedule, [])
    return repo, task


def test_a_second_reschedule_is_emailed_to_the_candidate():
    schedule, first, second = _schedule(), uuid4(), uuid4()
    first_key = notification_dedup_key(EmailTriggerEvent.INTERVIEW_RESCHEDULED, interview_schedule_id=schedule.id,
                                       event_id=first)
    sent = [SimpleNamespace(status=EmailNotificationStatus.SENT, dedup_key=first_key)]
    repo, task = _queue_candidate_reschedule(schedule, second, sent)
    assert task.apply_async.called
    assert repo.create_unless_duplicate.call_args.args[0].dedup_key.endswith(f"|event={second}")


def test_the_same_reschedule_is_not_emailed_twice():
    schedule, event = _schedule(), uuid4()
    key = notification_dedup_key(EmailTriggerEvent.INTERVIEW_RESCHEDULED, interview_schedule_id=schedule.id,
                                 event_id=event)
    repo, task = _queue_candidate_reschedule(
        schedule, event, [SimpleNamespace(status=EmailNotificationStatus.SENT, dedup_key=key)])
    assert not task.apply_async.called
    repo.create_unless_duplicate.assert_not_called()


@pytest.mark.parametrize("queue_name, trigger", [
    ("queue_interview_interviewer_rescheduled_email", EmailTriggerEvent.INTERVIEW_INTERVIEWER_RESCHEDULED),
    ("queue_interview_interviewer_cancelled_email", EmailTriggerEvent.INTERVIEW_INTERVIEWER_CANCELLED),
])
def test_interviewers_hear_about_every_reschedule_and_cancellation(queue_name, trigger):
    from app.services.notifications import interview_interviewer_lifecycle_emails as mod
    schedule, interviewer = _schedule(), SimpleNamespace(id=uuid4(), email="i@example.invalid", name="Pat",
                                                         timezone=None)
    earlier, latest = uuid4(), uuid4()
    earlier_key = notification_dedup_key(trigger, interview_schedule_id=schedule.id, interviewer_id=interviewer.id,
                                         event_id=earlier)
    repo = MagicMock()
    repo.get_by_interview_schedule_id_and_interviewer_id_and_trigger_event.return_value = [
        SimpleNamespace(status=EmailNotificationStatus.SENT, dedup_key=earlier_key)]
    repo.create_unless_duplicate.side_effect = lambda n: n
    with patch.object(mod, "interview_event_id", return_value=latest), \
            patch.object(mod, "EmailNotificationRepository", return_value=repo), \
            patch.object(mod, "EmailTemplateRepository", return_value=MagicMock()), \
            patch.object(mod, "send_candidate_email_task") as task:
        assert getattr(mod, queue_name)(MagicMock(), SimpleNamespace(id=uuid4()), schedule, interviewer) is True
    assert task.apply_async.called
    assert repo.create_unless_duplicate.call_args.args[0].dedup_key.endswith(f"|event={latest}")
