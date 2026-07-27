from unittest.mock import MagicMock
from uuid import uuid4

from app.models.email import EmailNotificationStatus, EmailTriggerEvent
from app.services.notifications.candidate_rejection_email_service import CandidateRejectionEmailService

"""
M07-E03 S02 T02: CandidateRejectionEmailService - queueing never touches a
plaintext email address (only opaque ids), and rendering only ever
substitutes candidate_name/job_title - never rejection internals.
"""


def make_service(active_template=None):
    email_template_repo = MagicMock()
    email_template_repo.get_active_by_trigger_event.return_value = active_template
    email_notification_repo = MagicMock()
    email_notification_repo.create.side_effect = lambda notification: notification
    service = CandidateRejectionEmailService(email_template_repo, email_notification_repo)
    return service, email_template_repo, email_notification_repo


def test_queue_rejection_email_creates_notification_when_template_active():
    template = MagicMock(id=uuid4())
    service, email_template_repo, email_notification_repo = make_service(active_template=template)
    candidate_id, campaign_candidate_id = uuid4(), uuid4()

    notification = service.queue_rejection_email(candidate_id, campaign_candidate_id)

    email_template_repo.get_active_by_trigger_event.assert_called_once_with(EmailTriggerEvent.CANDIDATE_REJECTED)
    assert notification is not None
    assert notification.candidate_id == candidate_id
    assert notification.campaign_candidate_id == campaign_candidate_id
    assert notification.template_id == template.id
    assert notification.status == EmailNotificationStatus.QUEUED
    assert notification.trigger_event == EmailTriggerEvent.CANDIDATE_REJECTED
    email_notification_repo.commit.assert_called_once()


def test_queue_rejection_email_returns_none_when_no_active_template():
    service, email_template_repo, email_notification_repo = make_service(active_template=None)

    notification = service.queue_rejection_email(uuid4(), uuid4())

    assert notification is None
    email_notification_repo.create.assert_not_called()
    email_notification_repo.commit.assert_not_called()


def test_render_content_only_substitutes_candidate_name_and_job_title():
    subject, body = CandidateRejectionEmailService.render_content(
        "Update on your application for {job_title}",
        "Dear {candidate_name}, thank you for applying to {job_title}.",
        candidate_full_name="Jane Doe",
        job_title="Senior Backend Engineer",
    )

    assert subject == "Update on your application for Senior Backend Engineer"
    assert body == "Dear Jane Doe, thank you for applying to Senior Backend Engineer."
    # Structurally cannot contain rejection internals - neither input
    # parameter carries anything but display-safe strings.
    for leaked_term in ("missing", "gap", "score", "threshold"):
        assert leaked_term not in body.lower()
