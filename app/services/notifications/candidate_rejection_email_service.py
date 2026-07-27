import logging
from uuid import UUID

from app.models.email import EmailNotification, EmailNotificationStatus, EmailTriggerEvent
from app.repositories.email_notification_repository import EmailNotificationRepository
from app.repositories.email_template_repository import EmailTemplateRepository

logger = logging.getLogger(__name__)


class CandidateRejectionEmailService:
    """
    M07-E03 S02 T02: business logic for queueing and rendering the
    candidate-rejection email. Structurally cannot leak rejection
    internals (missing skills/experience gap/education gap) - the only
    inputs it ever accepts are opaque ids and already-safe display strings
    (job_title, candidate_full_name) the caller resolves; no
    score_breakdown/rejection_reason ever crosses into this service.
    """

    def __init__(
        self,
        email_template_repo: EmailTemplateRepository,
        email_notification_repo: EmailNotificationRepository,
    ):
        self.email_template_repo = email_template_repo
        self.email_notification_repo = email_notification_repo

    def queue_rejection_email(
        self,
        candidate_id: UUID,
        campaign_candidate_id: UUID,
    ) -> EmailNotification | None:
        """
        Creates and commits an email_notifications row (status=QUEUED) for
        the active CANDIDATE_REJECTED template. Returns None (and logs an
        error) if no active template is configured - the caller must not
        enqueue an EMAIL_SEND task in that case, since there would be
        nothing to render.
        """
        template = self.email_template_repo.get_active_by_trigger_event(EmailTriggerEvent.CANDIDATE_REJECTED)
        if template is None:
            logger.error(
                "No active CANDIDATE_REJECTED email template configured - skipping rejection email | "
                "candidate_id=%s campaign_candidate_id=%s", candidate_id, campaign_candidate_id,
            )
            return None

        notification = self.email_notification_repo.create(EmailNotification(
            candidate_id=candidate_id,
            campaign_candidate_id=campaign_candidate_id,
            trigger_event=EmailTriggerEvent.CANDIDATE_REJECTED,
            template_id=template.id,
            status=EmailNotificationStatus.QUEUED,
        ))
        self.email_notification_repo.commit()
        return notification

    @staticmethod
    def render_content(
        subject_template: str,
        body_template: str,
        *,
        candidate_full_name: str,
        job_title: str,
    ) -> tuple[str, str]:
        """
        Only ever substitutes candidate_name/job_title - the template
        itself (seeded, admin-controlled) is the sole source of any other
        wording, so nothing rejection-specific can be introduced here.
        """
        context = {"candidate_name": candidate_full_name, "job_title": job_title}
        return subject_template.format(**context), body_template.format(**context)
