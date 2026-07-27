import logging
from datetime import datetime, timezone
from uuid import UUID

from app.core.celery_app import celery_app
from app.core.encryption_service import EncryptionService
from app.db.session import SessionLocal
from app.exceptions.email_exception import EmailDeliveryException
from app.models.async_tasks import FailureClassification, TaskStatus
from app.models.email import EmailNotificationStatus
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.candidate_repository import CandidateRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.dead_letter_queue_repository import DeadLetterQueueRepository
from app.repositories.email_notification_repository import EmailNotificationRepository
from app.repositories.email_template_repository import EmailTemplateRepository
from app.repositories.encryption_key_repository import EncryptionKeyRepository
from app.repositories.jd_repository import JDRepository
from app.services.celery_task_log_service import CeleryTaskLogService
from app.services.document_processing.error_classifier import classify
from app.services.document_processing.retry_policy import RetryPolicy, compute_backoff_seconds
from app.services.notifications.candidate_rejection_email_service import CandidateRejectionEmailService
from app.services.notifications.ses_email_client import SESEmailClient

logger = logging.getLogger(__name__)

EMAIL_SEND_TASK_TYPE = "EMAIL_SEND"

# M07-E03 S02 T03: a small, independent retry policy for a single-shot
# email send. Deliberately NOT RetryDriver - that's coupled to the
# multi-stage document-processing pipeline's StageExecutionError/
# checkpoint model, which doesn't fit a one-step send. Reuses the same
# underlying RetryPolicy/compute_backoff_seconds + DeadLetterQueueRepository
# + CeleryTaskLogService.mark_retry/mark_dead this codebase already uses
# for retry-then-dead-letter handling elsewhere.
_EMAIL_RETRY_POLICY = RetryPolicy(max_attempts=3, base_delay_seconds=10, max_delay_seconds=120)

# TODO(future story): if/when a daily rejection-email digest is specified,
# it belongs here as a separate Celery-beat task reading EmailNotification
# rows by trigger_event/status/date range. No digest framework exists
# anywhere in this codebase yet, so nothing beyond this note is implemented -
# per instruction, not inventing a new notification framework for it now.


@celery_app.task(name="notifications.send_candidate_email", bind=True)
def send_candidate_email_task(self, email_notification_id: str) -> None:
    """
    M07-E03 S02 T02/T03: sends ONE queued email_notifications row via SES.
    Runs fully independently of whatever queued it - the candidate_rejections
    record and the SCREENING -> REJECTED pipeline transition that triggered
    this are already committed in a separate, earlier transaction by the
    time this task starts, and nothing here can roll either of them back.
    """
    db = SessionLocal()
    task_id = self.request.id
    task_log = None
    notification = None
    attempt_number = self.request.retries + 1
    try:
        task_log_repo = CeleryTaskLogRepository(db)
        task_log_service = CeleryTaskLogService(task_log_repo)
        notification_repo = EmailNotificationRepository(db)
        template_repo = EmailTemplateRepository(db)
        candidate_repo = CandidateRepository(db)
        campaign_candidate_repo = CampaignCandidateRepository(db)
        campaign_repo = CampaignRepository(db)
        jd_repo = JDRepository(db)
        encryption_service = EncryptionService(EncryptionKeyRepository(db))
        dead_letter_queue_repo = DeadLetterQueueRepository(db)

        existing_task_log = task_log_repo.get_by_task_id(task_id)
        if existing_task_log is not None and existing_task_log.status == TaskStatus.SUCCESS:
            logger.info("EMAIL_SEND already completed for task_id=%s - skipping duplicate run.", task_id)
            return
        if existing_task_log is None:
            existing_task_log = task_log_service.create_log(task_id=task_id, task_type=EMAIL_SEND_TASK_TYPE)
        task_log = task_log_service.mark_running(existing_task_log)

        notification = notification_repo.get_by_id(UUID(email_notification_id))
        if notification is None:
            raise ValueError(f"EmailNotification '{email_notification_id}' not found.")

        if notification.status == EmailNotificationStatus.SENT:
            task_log_service.mark_success(task_log, summary="Notification already SENT.")
            return

        template = template_repo.get_by_id(notification.template_id)
        if template is None:
            raise ValueError(f"EmailTemplate '{notification.template_id}' not found.")

        candidate = candidate_repo.get_by_id(notification.candidate_id)
        if candidate is None:
            raise ValueError(f"Candidate '{notification.candidate_id}' not found.")

        # Never use campaign name - JobDescription.title only. Falls back
        # to a generic phrase only if the chain is somehow unresolvable
        # (e.g. campaign/JD deleted after the notification was queued).
        job_title = "the position you applied for"
        if notification.campaign_candidate_id is not None:
            campaign_candidate = campaign_candidate_repo.get_by_id(notification.campaign_candidate_id)
            if campaign_candidate is not None:
                campaign = campaign_repo.get_by_id(campaign_candidate.campaign_id)
                if campaign is not None:
                    job_description = jd_repo.get_by_id(campaign.jd_id)
                    if job_description is not None:
                        job_title = job_description.title

        # Decrypt only here, at send time - never persisted anywhere.
        to_address = encryption_service.decrypt(candidate.email_encrypted, candidate.encryption_key_id)
        candidate_full_name = encryption_service.decrypt(candidate.full_name_encrypted, candidate.encryption_key_id)

        subject, body = CandidateRejectionEmailService.render_content(
            template.subject, template.body_template,
            candidate_full_name=candidate_full_name, job_title=job_title,
        )

        SESEmailClient().send_email(to_address=to_address, subject=subject, body_text=body)

        notification.status = EmailNotificationStatus.SENT
        notification.sent_at = datetime.now(timezone.utc)
        notification_repo.update(notification)
        notification_repo.commit()

        task_log_service.mark_success(task_log, summary=f"Sent to candidate_id={notification.candidate_id}.")

    except Exception as ex:
        db.rollback()
        original = ex.original if isinstance(ex, EmailDeliveryException) else ex
        classification = classify(original)

        if classification != FailureClassification.PERMANENT and attempt_number < _EMAIL_RETRY_POLICY.max_attempts:
            if task_log:
                task_log_service.mark_retry(task_log)
            delay = compute_backoff_seconds(_EMAIL_RETRY_POLICY, attempt_number)
            logger.warning(
                "EMAIL_SEND transient failure, retrying | email_notification_id=%s attempt=%s delay=%ss error=%s",
                email_notification_id, attempt_number, delay, ex,
            )
            self.retry(exc=ex, countdown=delay, max_retries=_EMAIL_RETRY_POLICY.max_attempts)
            return

        # T03: retries exhausted (or a permanent/business failure) -
        # dead-letter + mark the notification FAILED. This never rolls back
        # the rejection record or the pipeline transition - those already
        # committed in a separate, earlier transaction.
        error_message = str(ex)
        dead_letter_queue_repo.create(
            original_task_id=task_id,
            task_type=EMAIL_SEND_TASK_TYPE,
            final_error_message=error_message,
            full_error_trace=None,
            input_payload={"email_notification_id": email_notification_id},
            retry_count=attempt_number,
            first_attempted_at=task_log.queued_at if task_log else datetime.now(timezone.utc),
            last_attempted_at=datetime.now(timezone.utc),
            campaign_candidate_id=notification.campaign_candidate_id if notification else None,
        )
        dead_letter_queue_repo.commit()

        if notification is not None:
            notification.status = EmailNotificationStatus.FAILED
            notification.error_reason = error_message
            notification_repo.update(notification)
            notification_repo.commit()

        if task_log:
            task_log_service.mark_dead(task_log, error_message)
        logger.exception("EMAIL_SEND permanently failed | email_notification_id=%s", email_notification_id)
        # Deliberately not re-raised: this is now dead-lettered/terminal
        # bookkeeping, not an unhandled Celery-level failure.

    finally:
        db.close()
