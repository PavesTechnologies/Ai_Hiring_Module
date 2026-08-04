from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models.email import EmailNotification, EmailTriggerEvent


class EmailNotificationRepository:

    def __init__(self, db: Session):
        self.db = db

    def create(self, notification: EmailNotification) -> EmailNotification:
        self.db.add(notification)
        self.db.flush()
        self.db.refresh(notification)
        return notification

    def get_by_id(self, notification_id: UUID) -> EmailNotification | None:
        return (
            self.db.query(EmailNotification)
            .filter(EmailNotification.id == notification_id)
            .first()
        )

    def get_by_campaign_candidate_id_and_trigger_event(
        self, campaign_candidate_id: UUID, trigger_event: EmailTriggerEvent,
    ) -> list[EmailNotification]:
        """
        Story 542: dedup lookup for CandidateRejectionEmailService -
        every notification of this trigger_event already queued/sent for
        one campaign_candidate, so a caller (deterministic OR semantic
        rejection) never queues a second one.
        """
        return (
            self.db.query(EmailNotification)
            .filter(
                EmailNotification.campaign_candidate_id == campaign_candidate_id,
                EmailNotification.trigger_event == trigger_event,
            )
            .all()
        )

    def update(self, notification: EmailNotification) -> EmailNotification:
        self.db.flush()
        self.db.refresh(notification)
        return notification

    def delete_by_candidate(self, candidate_id: UUID) -> None:
        """Candidate erasure — removes email_notifications rows (candidate_id is a required FK)."""
        self.db.execute(delete(EmailNotification).where(EmailNotification.candidate_id == candidate_id))
        self.db.flush()

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
