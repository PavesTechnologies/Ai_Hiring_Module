"""Resolving a stuck email against real Postgres (rolled back): the status change and its audit entry land."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select

from app.models.compliance import AuditLog
from app.models.email import EmailNotification, EmailNotificationStatus, EmailRecipientType, EmailTemplate, \
    EmailTriggerEvent
from app.models.identity import User
from app.repositories.audit_repository import AuditRepository
from app.services.audit_service import AuditService
from app.services.notifications.stuck_sends import list_stuck, resolve_stuck
from tests.db.test_campaign_candidate_delete_scored import db  # noqa: F401 - fixture


def test_a_stuck_email_is_listed_then_resolved_with_an_audit_entry(db):  # noqa: F811
    user = db.execute(select(User).limit(1)).scalar_one()
    template = EmailTemplate(id=uuid4(), trigger_event=EmailTriggerEvent.EMAIL_SEND_STUCK_ALERT, name="zz test",
                             subject="s", body_template="b", is_active=False)
    db.add(template)
    db.flush()
    row = EmailNotification(recipient_type=EmailRecipientType.INTERNAL_USER, recipient_user_id=user.id,
                            recipient_email="hr@example.invalid", recipient_name="HR",
                            trigger_event=EmailTriggerEvent.EMAIL_SEND_STUCK_ALERT, template_id=template.id,
                            status=EmailNotificationStatus.SENDING, provider_message_id="ses-xyz",
                            sending_since=datetime.now(timezone.utc) - timedelta(minutes=45))
    db.add(row)
    db.flush()
    assert str(row.id) in {item["id"] for item in list_stuck(db)}

    db.commit = db.flush        # inside the rolled-back fixture
    resolve_stuck(db, row.id, outcome="SENT", actor_id=user.id, actor_role="HR_ADMIN",
                  audit_service=AuditService(AuditRepository(db)))
    db.refresh(row)
    assert row.status == EmailNotificationStatus.SENT and row.sent_at is not None
    entry = db.execute(select(AuditLog).where(AuditLog.entity_id == row.id)).scalar_one()
    assert entry.action_type.value == "EMAIL_SEND_RESOLVED" and entry.detail["provider_message_id"] == "ses-xyz"
