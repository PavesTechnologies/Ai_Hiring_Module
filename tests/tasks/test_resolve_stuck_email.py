"""
An HR admin resolves an email stuck in SENDING as sent or failed, after checking the email provider:
only a SENDING row, audited, nothing sent. The list shows no recipient. No email.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.models.email import EmailNotificationStatus, EmailRecipientType, EmailTriggerEvent
from app.services.notifications.stuck_sends import StuckEmailError, list_stuck, resolve_stuck


def _row(status=EmailNotificationStatus.SENDING):
    return SimpleNamespace(id=uuid4(), status=status, trigger_event=EmailTriggerEvent.CANDIDATE_REJECTED,
                           recipient_type=EmailRecipientType.CANDIDATE, campaign_candidate_id=uuid4(),
                           sending_since=datetime.now(timezone.utc) - timedelta(minutes=50), created_at=None,
                           provider_message_id="ses-1", error_reason=None, sent_at=None,
                           recipient_email="jane@example.invalid")


def _db(row):
    db = MagicMock()
    db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = row
    return db


@pytest.mark.parametrize("outcome, status", [("SENT", EmailNotificationStatus.SENT),
                                             ("failed", EmailNotificationStatus.FAILED)])
def test_a_stuck_email_is_resolved_and_audited(outcome, status):
    row, audit = _row(), MagicMock()
    db = _db(row)
    result = resolve_stuck(db, row.id, outcome=outcome, note="Checked SES: delivered", actor_id="hr-1",
                           actor_role="HR_ADMIN", audit_service=audit)
    assert row.status == status and result["status"] == status.value
    assert (row.sent_at is not None) == (status == EmailNotificationStatus.SENT)
    details = audit.log.call_args.kwargs["details"]
    assert audit.log.call_args.kwargs["action_type"] == "EMAIL_SEND_RESOLVED"
    assert details["outcome"] == outcome.upper() and details["provider_message_id"] == "ses-1"
    assert "jane@example.invalid" not in str(details)
    db.commit.assert_called_once()


@pytest.mark.parametrize("row, outcome, code", [
    (_row(EmailNotificationStatus.SENT), "SENT", 409),      # only a SENDING row
    (None, "SENT", 404),
    (_row(), "RESEND", 422),                                # no other outcome
])
def test_what_cannot_be_resolved(row, outcome, code):
    audit = MagicMock()
    with pytest.raises(StuckEmailError) as caught:
        resolve_stuck(_db(row), uuid4(), outcome=outcome, note=None, actor_id="hr", actor_role="HR_ADMIN",
                      audit_service=audit)
    assert caught.value.status_code == code
    audit.log.assert_not_called()


def test_the_list_has_no_recipient():
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [_row()]
    [item] = list_stuck(db)
    assert item["provider_message_id"] == "ses-1" and item["minutes"] >= 49
    assert "jane@example.invalid" not in str(item)


def test_the_routes_are_hr_admin_only():
    from app.api.routes import scoring_monitoring_routes as mod
    from fastapi.params import Security as SecurityParam  # noqa: F401
    paths = {r.path: r for r in mod.router.routes}
    assert "/monitoring/stuck-emails" in paths and "/monitoring/stuck-emails/{notification_id}/resolve" in paths
    import inspect
    src = inspect.getsource(mod.post_resolve_stuck_email)
    assert "require_roles(UserRole.HR_ADMIN)" in src
