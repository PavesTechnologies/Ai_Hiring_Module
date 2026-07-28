from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError

from app.exceptions.email_exception import EmailDeliveryException
from app.models.async_tasks import TaskStatus
from app.models.email import EmailNotificationStatus

TASKS_MODULE = "app.tasks.email_tasks"


def _make_notification(status=EmailNotificationStatus.QUEUED, campaign_candidate_id=None):
    return SimpleNamespace(
        id=uuid4(), candidate_id=uuid4(), campaign_candidate_id=campaign_candidate_id or uuid4(),
        template_id=uuid4(), status=status, sent_at=None, error_reason=None,
    )


def _make_template():
    return SimpleNamespace(id=uuid4(), subject="Update on your application for {job_title}", body_template="Dear {candidate_name}.")


def _make_candidate():
    return SimpleNamespace(id=uuid4(), email_encrypted=b"enc-email", full_name_encrypted=b"enc-name", encryption_key_id=uuid4())


class _Harness:
    """Mirrors the pattern already established in test_deterministic_scoring_tasks.py's _Harness."""

    def __init__(self):
        self.task_log_repo = MagicMock()
        self.task_log_repo.get_by_task_id.return_value = None
        self.task_log_repo.queued_at = None
        self.notification_repo = MagicMock()
        self.template_repo = MagicMock()
        self.candidate_repo = MagicMock()
        self.campaign_candidate_repo = MagicMock()
        self.campaign_repo = MagicMock()
        self.jd_repo = MagicMock()
        self.dead_letter_queue_repo = MagicMock()
        self.encryption_service_instance = MagicMock()
        self.encryption_service_instance.decrypt.side_effect = ["candidate@example.com", "Jane Doe"]
        self.ses_client_instance = MagicMock()

    def __enter__(self):
        self._patches = [
            patch(f"{TASKS_MODULE}.SessionLocal", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.CeleryTaskLogRepository", return_value=self.task_log_repo),
            patch(f"{TASKS_MODULE}.EmailNotificationRepository", return_value=self.notification_repo),
            patch(f"{TASKS_MODULE}.EmailTemplateRepository", return_value=self.template_repo),
            patch(f"{TASKS_MODULE}.CandidateRepository", return_value=self.candidate_repo),
            patch(f"{TASKS_MODULE}.CampaignCandidateRepository", return_value=self.campaign_candidate_repo),
            patch(f"{TASKS_MODULE}.CampaignRepository", return_value=self.campaign_repo),
            patch(f"{TASKS_MODULE}.JDRepository", return_value=self.jd_repo),
            patch(f"{TASKS_MODULE}.EncryptionKeyRepository", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.EncryptionService", return_value=self.encryption_service_instance),
            patch(f"{TASKS_MODULE}.DeadLetterQueueRepository", return_value=self.dead_letter_queue_repo),
            patch(f"{TASKS_MODULE}.SESEmailClient", return_value=self.ses_client_instance),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


def test_sends_email_successfully_and_marks_notification_sent():
    from app.tasks.email_tasks import send_candidate_email_task

    with _Harness() as h:
        notification = _make_notification()
        h.notification_repo.get_by_id.return_value = notification
        h.template_repo.get_by_id.return_value = _make_template()
        h.candidate_repo.get_by_id.return_value = _make_candidate()
        h.campaign_candidate_repo.get_by_id.return_value = SimpleNamespace(campaign_id=uuid4())
        h.campaign_repo.get_by_id.return_value = SimpleNamespace(jd_id=uuid4())
        h.jd_repo.get_by_id.return_value = SimpleNamespace(title="Backend Engineer")
        h.ses_client_instance.send_email.return_value = "ses-message-id"

        send_candidate_email_task(email_notification_id=str(notification.id))

        h.ses_client_instance.send_email.assert_called_once_with(
            to_address="candidate@example.com",
            subject="Update on your application for Backend Engineer",
            body_text="Dear Jane Doe.",
        )
        assert notification.status == EmailNotificationStatus.SENT
        assert notification.sent_at is not None
        h.notification_repo.commit.assert_called_once()
        h.dead_letter_queue_repo.create.assert_not_called()


def test_skips_when_notification_already_sent():
    from app.tasks.email_tasks import send_candidate_email_task

    with _Harness() as h:
        notification = _make_notification(status=EmailNotificationStatus.SENT)
        h.notification_repo.get_by_id.return_value = notification

        send_candidate_email_task(email_notification_id=str(notification.id))

        h.ses_client_instance.send_email.assert_not_called()


def test_skips_duplicate_run_when_task_log_already_success():
    from app.tasks.email_tasks import send_candidate_email_task

    with _Harness() as h:
        h.task_log_repo.get_by_task_id.return_value = SimpleNamespace(status=TaskStatus.SUCCESS)

        send_candidate_email_task(email_notification_id=str(uuid4()))

        h.notification_repo.get_by_id.assert_not_called()
        h.ses_client_instance.send_email.assert_not_called()


def test_retries_on_transient_ses_failure():
    """
    Throttling is TRANSIENT (see error_classifier) - must retry, never
    dead-letter on the first attempt. Celery's Task.retry(exc=ex) re-raises
    the ORIGINAL exception (not a generic Retry) so that a real worker's
    own execution wrapper can catch it and requeue - calling the task as a
    plain function here (no worker context) surfaces that same re-raise.
    """
    from app.tasks.email_tasks import send_candidate_email_task

    with _Harness() as h:
        notification = _make_notification()
        h.notification_repo.get_by_id.return_value = notification
        h.template_repo.get_by_id.return_value = _make_template()
        h.candidate_repo.get_by_id.return_value = _make_candidate()
        h.campaign_candidate_repo.get_by_id.return_value = None  # job_title falls back, irrelevant here

        client_error = ClientError({"Error": {"Code": "Throttling", "Message": "Rate exceeded"}}, "SendEmail")
        h.ses_client_instance.send_email.side_effect = EmailDeliveryException("SES send_email failed", client_error)

        with pytest.raises(EmailDeliveryException):
            send_candidate_email_task(email_notification_id=str(notification.id))

        h.dead_letter_queue_repo.create.assert_not_called()
        assert notification.status == EmailNotificationStatus.QUEUED  # unchanged - not yet terminal


def test_dead_letters_after_permanent_ses_failure():
    """MessageRejected (bad/unverified address) is PERMANENT - dead-letter immediately, no retry."""
    from app.tasks.email_tasks import send_candidate_email_task

    with _Harness() as h:
        notification = _make_notification()
        h.notification_repo.get_by_id.return_value = notification
        h.template_repo.get_by_id.return_value = _make_template()
        h.candidate_repo.get_by_id.return_value = _make_candidate()
        h.campaign_candidate_repo.get_by_id.return_value = None

        client_error = ClientError({"Error": {"Code": "MessageRejected", "Message": "Bad address"}}, "SendEmail")
        h.ses_client_instance.send_email.side_effect = EmailDeliveryException("SES send_email failed", client_error)

        send_candidate_email_task(email_notification_id=str(notification.id))

        h.dead_letter_queue_repo.create.assert_called_once()
        create_kwargs = h.dead_letter_queue_repo.create.call_args.kwargs
        assert create_kwargs["task_type"] == "EMAIL_SEND"
        assert create_kwargs["campaign_candidate_id"] == notification.campaign_candidate_id
        assert notification.status == EmailNotificationStatus.FAILED
        assert notification.error_reason is not None
