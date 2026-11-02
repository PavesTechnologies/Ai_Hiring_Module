"""
One email path: every email is an email_notifications row sent by notifications.send_candidate_email,
so it is recorded and governed by EMAIL_DELIVERY.

- Only app/tasks/email_tasks.py uses the SES client; nothing in app/ creates an SES client itself.
- Internal users (e.g. HR_ADMINs for the embedding health alert) are INTERNAL_USER notifications.
- With EMAIL_DELIVERY=log a notification ends LOGGED, never SENT.
"""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.models.email import EmailNotificationStatus, EmailRecipientType, EmailTriggerEvent
from tests.tasks.test_email_tasks import _Harness, _make_notification

ROOT = Path(__file__).resolve().parents[2]
SENDER = "app/tasks/email_tasks.py"
CLIENT = "app/services/notifications/ses_email_client.py"


def _app_modules():
    return [p for p in (ROOT / "app").rglob("*.py") if "__pycache__" not in p.parts]


def test_only_the_send_task_uses_the_ses_client():
    users = []
    for path in _app_modules():
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("ses_email_client"):
                if any(a.name == "SESEmailClient" for a in node.names):
                    users.append(rel)
            if isinstance(node, ast.Name) and node.id == "SESEmailClient" and rel not in (SENDER, CLIENT):
                users.append(rel)
    assert sorted(set(users)) == [SENDER]


def test_nothing_else_creates_an_ses_client():
    makers = []
    for path in _app_modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            # boto3.client("ses", ...) / session.client("sesv2", ...)
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "client"
                    and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value in ("ses", "sesv2")):
                makers.append(path.relative_to(ROOT).as_posix())
    assert sorted(set(makers)) == [CLIENT]


# --- the send task -------------------------------------------------------------------------

def _internal(status=EmailNotificationStatus.QUEUED):
    return _make_notification(status=status, recipient_type=EmailRecipientType.INTERNAL_USER,
                              recipient_email="hr@example.invalid",
                              trigger_event=EmailTriggerEvent.EMBEDDING_HEALTH_ALERT,
                              template_context={"campaign_name": "Backend", "recipient_name": "HR"})


def _run(h, notification, message_id):
    from app.tasks.email_tasks import send_candidate_email_task
    h.notification_repo.get_by_id.return_value = notification
    h.template_repo.get_by_id.return_value = SimpleNamespace(
        id=uuid4(), subject="Alert: {campaign_name}", body_template="Hello {recipient_name}, {campaign_name}.")
    h.campaign_candidate_repo.get_by_id.return_value = None
    h.ses_client_instance.send_email.return_value = message_id
    send_candidate_email_task.apply(kwargs={"email_notification_id": str(notification.id)})


def test_an_internal_user_email_goes_to_the_snapshot_address_and_is_sent():
    with _Harness() as h:
        notification = _internal()
        _run(h, notification, "ses-message-id")
        kwargs = h.ses_client_instance.send_email.call_args.kwargs
        assert kwargs["to_address"] == "hr@example.invalid" and kwargs["subject"] == "Alert: Backend"
        assert notification.status == EmailNotificationStatus.SENT and notification.sent_at is not None
        h.encryption_service_instance.decrypt.assert_not_called()       # no candidate involved


def test_in_log_mode_the_notification_is_recorded_as_logged_not_sent():
    with _Harness() as h:
        notification = _internal()
        _run(h, notification, "log-only-123")
        assert notification.status == EmailNotificationStatus.LOGGED


def test_a_logged_notification_is_never_sent_again():
    with _Harness() as h:
        notification = _internal(status=EmailNotificationStatus.LOGGED)
        _run(h, notification, "ses-message-id")
        h.ses_client_instance.send_email.assert_not_called()


def test_the_real_client_in_log_mode_returns_a_log_only_id_without_an_ses_client():
    from app.services.notifications.ses_email_client import LOG_ONLY_PREFIX, SESEmailClient
    with patch("app.services.notifications.ses_email_client.settings", SimpleNamespace(email_delivery="log")), \
            patch("boto3.client", side_effect=AssertionError("SES client created")):
        assert SESEmailClient().send_email(to_address="x@example.invalid", subject="s",
                                           body_text="b").startswith(LOG_ONLY_PREFIX)


# --- queueing for internal users ------------------------------------------------------------

MODULE = "app.services.notifications.internal_user_emails"


def _queue(users, template=SimpleNamespace(id=uuid4()), opted_out=()):
    from app.services.notifications.internal_user_emails import queue_internal_user_emails
    repo = MagicMock()
    repo.create.side_effect = lambda n: (setattr(n, "id", uuid4()), n)[1]
    send = MagicMock()
    with patch(f"{MODULE}.EmailTemplateRepository") as templates, \
            patch(f"{MODULE}.EmailNotificationRepository", return_value=repo), \
            patch(f"{MODULE}.is_notification_enabled", side_effect=lambda db, uid, ev: uid not in opted_out), \
            patch(f"{MODULE}.send_candidate_email_task", send):
        templates.return_value.get_active_by_trigger_event.return_value = template
        count = queue_internal_user_emails(MagicMock(), users, EmailTriggerEvent.EMBEDDING_HEALTH_ALERT,
                                           {"campaign_name": "Backend"})
    return count, repo, send


def _user(uid, email="hr@example.invalid", name="HR One"):
    return SimpleNamespace(id=uid, email=email, full_name=name)


def test_one_internal_user_notification_per_user_then_dispatched():
    count, repo, send = _queue([_user("u1"), _user("u2", "hr2@example.invalid", "HR Two")])
    assert count == 2
    rows = [c.args[0] for c in repo.create.call_args_list]
    assert {r.recipient_type for r in rows} == {EmailRecipientType.INTERNAL_USER}
    assert [(r.recipient_user_id, r.recipient_email, r.recipient_name) for r in rows] == [
        ("u1", "hr@example.invalid", "HR One"), ("u2", "hr2@example.invalid", "HR Two")]
    assert all(r.candidate_id is None and r.interview_interviewer_id is None for r in rows)
    assert rows[0].template_context == {"recipient_name": "HR One", "campaign_name": "Backend"}
    assert all(r.status == EmailNotificationStatus.QUEUED for r in rows)
    repo.commit.assert_called_once()
    assert send.apply_async.call_count == 2


def test_opted_out_users_and_users_without_an_address_are_skipped():
    count, repo, send = _queue([_user("u1"), _user("u2"), _user("u3", email=None)], opted_out={"u2"})
    assert count == 1 and [c.args[0].recipient_user_id for c in repo.create.call_args_list] == ["u1"]


def test_without_an_active_template_nothing_is_queued():
    count, repo, send = _queue([_user("u1")], template=None)
    assert count == 0
    repo.create.assert_not_called()
    send.apply_async.assert_not_called()
