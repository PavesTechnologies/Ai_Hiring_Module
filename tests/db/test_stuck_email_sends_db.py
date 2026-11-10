"""Batch 4 follow-up, against real Postgres (rolled back): sending_since, the stuck query, the alert template."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select, update

from app.models.email import EmailNotification, EmailNotificationStatus, EmailRecipientType, EmailTemplate, \
    EmailTriggerEvent
from app.models.identity import User
from app.repositories.email_notification_repository import EmailNotificationRepository
from tests.db.test_campaign_candidate_delete_scored import db  # noqa: F401 - fixture


def _row(db, trigger=EmailTriggerEvent.EMAIL_SEND_STUCK_ALERT):  # noqa: F811
    user = db.execute(select(User).limit(1)).scalar_one()
    template = EmailTemplate(id=uuid4(), trigger_event=trigger, name="zz test", subject="s", body_template="b",
                             is_active=False)
    db.add(template)
    db.flush()
    row = EmailNotification(recipient_type=EmailRecipientType.INTERNAL_USER, recipient_user_id=user.id,
                            recipient_email="hr@example.invalid", recipient_name="HR", trigger_event=trigger,
                            template_id=template.id, status=EmailNotificationStatus.QUEUED)
    db.add(row)
    db.flush()
    return row


def _no_commit(db):  # noqa: F811
    # claim/release commit; inside the rolled-back fixture a flush is enough.
    db.commit = db.flush
    return EmailNotificationRepository(db)


def test_a_claim_records_when_and_a_release_clears_it(db):  # noqa: F811
    row, repo = _row(db), _no_commit(db)
    assert repo.claim_for_sending(row.id)
    db.refresh(row)
    assert row.status == EmailNotificationStatus.SENDING and row.sending_since is not None
    repo.release_claim(row.id)
    db.refresh(row)
    assert row.status == EmailNotificationStatus.QUEUED and row.sending_since is None


def test_only_rows_sending_past_the_cutoff_are_stuck(db):  # noqa: F811
    stuck, fresh, repo = _row(db), _row(db), _no_commit(db)
    now = datetime.now(timezone.utc)
    for row, minutes in ((stuck, 45), (fresh, 5)):
        db.execute(update(EmailNotification).where(EmailNotification.id == row.id).values(
            status=EmailNotificationStatus.SENDING, sending_since=now - timedelta(minutes=minutes)))
    db.flush()
    found = {t: n for t, n, *_ in repo.get_stuck_sending(now - timedelta(minutes=30))}
    mine = db.execute(select(EmailNotification.id).where(
        EmailNotification.status == EmailNotificationStatus.SENDING,
        EmailNotification.sending_since < now - timedelta(minutes=30))).scalars().all()
    assert stuck.id in mine and fresh.id not in mine
    assert found.get(EmailTriggerEvent.EMAIL_SEND_STUCK_ALERT, 0) >= 1
    excluded = repo.get_stuck_sending(now - timedelta(minutes=30),
                                      exclude_triggers=(EmailTriggerEvent.EMAIL_SEND_STUCK_ALERT,))
    assert EmailTriggerEvent.EMAIL_SEND_STUCK_ALERT not in {t for t, *_ in excluded}


def test_the_seeded_stuck_alert_template_renders_with_what_the_task_supplies(db):  # noqa: F811
    from app.repositories.email_template_repository import EmailTemplateRepository
    template = EmailTemplateRepository(db).get_active_by_trigger_event(EmailTriggerEvent.EMAIL_SEND_STUCK_ALERT)
    assert template is not None, "run app.seeds.seed_email_templates"
    context = {"candidate_name": "the candidate", "job_title": "the position you applied for",  # send-task keys
               "recipient_name": "HR One", "stuck_count": 3, "stuck_after_minutes": 30, "oldest_minutes": 95,
               "by_trigger": "CANDIDATE_REJECTED (3)", "monitoring_link": "https://example.invalid/ai-screening/dashboard"}
    assert template.subject.format(**context) == "Email alert: 3 email(s) stuck sending"
    body = template.body_template.format(**context)
    assert "3 (sending for over 30 minutes)" in body and "95 minutes" in body and "CANDIDATE_REJECTED (3)" in body
