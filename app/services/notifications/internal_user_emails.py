"""
Queue-and-dispatch for emails to platform users (INTERNAL_USER recipients) - ops alerts such as
the embedding health alert. Same shape as interview_feedback_request_emails.py: template lookup,
one email_notifications row per recipient (committed), then notifications.send_candidate_email
for each - so these are recorded and governed by EMAIL_DELIVERY like every other email, instead
of calling SES directly.

A user's opt-out (user_notification_preferences) is honoured.
"""
import logging

from app.models.email import EmailNotification, EmailNotificationStatus, EmailRecipientType, EmailTriggerEvent
from app.repositories.email_notification_repository import EmailNotificationRepository
from app.repositories.email_template_repository import EmailTemplateRepository
from app.services.notifications.user_notification_preferences import is_notification_enabled
from app.tasks.email_tasks import send_candidate_email_task

logger = logging.getLogger(__name__)


def queue_internal_user_emails(db, users, trigger_event: EmailTriggerEvent, template_context: dict) -> int:
    """
    One notification per user (each with an email address and not opted out), then dispatched.
    Returns how many were queued. Never raises: a failure is logged and counts as none queued.
    """
    try:
        template = EmailTemplateRepository(db).get_active_by_trigger_event(trigger_event)
        if template is None:
            logger.error("No active %s email template configured - nothing queued.", trigger_event.value)
            return 0
        notification_repo = EmailNotificationRepository(db)
        queued = []
        for user in users:
            if not getattr(user, "email", None):
                continue
            if not is_notification_enabled(db, user.id, trigger_event):
                logger.info("%s skipped: user_id=%s opted out.", trigger_event.value, user.id)
                continue
            queued.append(notification_repo.create(EmailNotification(
                candidate_id=None,
                campaign_candidate_id=None,
                interview_interviewer_id=None,
                recipient_type=EmailRecipientType.INTERNAL_USER,
                recipient_user_id=user.id,
                recipient_email=user.email,
                recipient_name=getattr(user, "full_name", None) or user.email,
                trigger_event=trigger_event,
                template_id=template.id,
                status=EmailNotificationStatus.QUEUED,
                template_context={"recipient_name": getattr(user, "full_name", None) or user.email,
                                  **template_context},
            )))
        notification_repo.commit()
    except Exception:
        logger.exception("Failed to queue %s emails.", trigger_event.value)
        try:
            db.rollback()
        except Exception:
            logger.exception("Rollback after a failed %s queue failed.", trigger_event.value)
        return 0

    for notification in queued:
        try:
            send_candidate_email_task.apply_async(kwargs={"email_notification_id": str(notification.id)})
        except Exception:
            # The row stays QUEUED - recorded, and visible as never dispatched.
            logger.exception("Dispatching %s email %s failed.", trigger_event.value, notification.id)
    return len(queued)
