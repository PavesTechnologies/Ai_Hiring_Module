"""INTERNAL_USER notifications against real Postgres (rolled back): the recipient-shape check and LOGGED."""
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.email import (
    EmailNotification, EmailNotificationStatus, EmailRecipientType, EmailTemplate, EmailTriggerEvent,
)
from app.models.identity import User
from tests.db.test_campaign_candidate_delete_scored import db  # noqa: F401 - fixture


def _template(db):  # noqa: F811
    template = EmailTemplate(id=uuid4(), trigger_event=EmailTriggerEvent.EMBEDDING_HEALTH_ALERT,
                             name="zz test", subject="s", body_template="b", is_active=False)
    db.add(template)
    db.flush()
    return template


def _user(db):  # noqa: F811
    return db.execute(select(User).limit(1)).scalar_one()


def test_an_internal_user_notification_fits_the_check_and_can_be_logged(db):  # noqa: F811
    user, template = _user(db), _template(db)
    row = EmailNotification(recipient_type=EmailRecipientType.INTERNAL_USER, recipient_user_id=user.id,
                            recipient_email="hr@example.invalid", recipient_name="HR",
                            trigger_event=EmailTriggerEvent.EMBEDDING_HEALTH_ALERT, template_id=template.id,
                            status=EmailNotificationStatus.QUEUED, template_context={"campaign_name": "x"})
    db.add(row)
    db.flush()
    row.status = EmailNotificationStatus.LOGGED
    db.flush()
    db.refresh(row)
    assert row.status == EmailNotificationStatus.LOGGED


@pytest.mark.parametrize("broken", [
    {"recipient_user_id": None},                                   # an internal user needs the user
    {"recipient_email": None},                                     # ... and the address snapshot
    {"interview_interviewer_id": uuid4()},                         # never an interviewer too
])
def test_a_malformed_internal_user_row_is_refused(db, broken):  # noqa: F811
    user, template = _user(db), _template(db)
    values = dict(recipient_type=EmailRecipientType.INTERNAL_USER, recipient_user_id=user.id,
                  recipient_email="hr@example.invalid", recipient_name="HR",
                  trigger_event=EmailTriggerEvent.EMBEDDING_HEALTH_ALERT, template_id=template.id,
                  status=EmailNotificationStatus.QUEUED)
    values.update(broken)
    db.add(EmailNotification(**values))
    with pytest.raises(IntegrityError):
        db.flush()


def test_the_seeded_health_alert_template_renders_with_what_the_task_supplies(db):  # noqa: F811
    from app.repositories.email_template_repository import EmailTemplateRepository
    template = EmailTemplateRepository(db).get_active_by_trigger_event(EmailTriggerEvent.EMBEDDING_HEALTH_ALERT)
    assert template is not None, "run app.seeds.seed_email_templates"
    context = {"candidate_name": "the candidate", "job_title": "the position you applied for",  # send-task keys
               "recipient_name": "HR One", "campaign_name": "Backend", "affected_count": 3,
               "total_screening_count": 4, "failure_percentage": "75.00", "circuit_breaker_state": "OPEN",
               "monitoring_link": "https://example.invalid/campaigns/1", "suspended_note": "SUSPENDED"}
    assert template.subject.format(**context) == "Embedding health alert: campaign 'Backend'"
    body = template.body_template.format(**context)
    assert "3 of 4 in SCREENING" in body and "75.00%" in body and "SUSPENDED" in body and "HR One" in body
