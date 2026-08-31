from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models.email import EmailNotification, EmailRecipientType, EmailTriggerEvent


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

    def get_by_interview_schedule_id_and_trigger_event(
        self, interview_schedule_id: UUID, trigger_event: EmailTriggerEvent,
    ) -> list[EmailNotification]:
        """
        Epic 5 Step 2 - dedup lookup for INTERVIEW_SCHEDULED/RESCHEDULED/
        CANCELLED. Scoped to one interview round, NOT
        get_by_campaign_candidate_id_and_trigger_event's campaign-
        candidate-wide scope - these 3 trigger events are not terminal
        the way CANDIDATE_REJECTED/CANDIDATE_SELECTED are (a candidate can
        have many rounds), so deduping by campaign_candidate_id alone
        would silently skip round 2's "interview scheduled" email because
        round 1's already satisfied that check.
        """
        return (
            self.db.query(EmailNotification)
            .filter(
                EmailNotification.interview_schedule_id == interview_schedule_id,
                EmailNotification.trigger_event == trigger_event,
            )
            .all()
        )

    def get_by_interview_schedule_id_and_interviewer_id_and_trigger_event(
        self, interview_schedule_id: UUID, interviewer_id: UUID, trigger_event: EmailTriggerEvent,
    ) -> list[EmailNotification]:
        """
        Epic 5 Step 4 - dedup lookup for INTERVIEW_FEEDBACK_REQUESTED.
        Scoped per interviewer, NOT just per round like
        get_by_interview_schedule_id_and_trigger_event above - a round
        can have several interviewers, each getting their own
        independent email/token, so deduping by round alone would
        incorrectly treat interviewer A's already-sent email as blocking
        interviewer B's.
        """
        return (
            self.db.query(EmailNotification)
            .filter(
                EmailNotification.interview_schedule_id == interview_schedule_id,
                EmailNotification.interview_interviewer_id == interviewer_id,
                EmailNotification.trigger_event == trigger_event,
            )
            .all()
        )

    def update(self, notification: EmailNotification) -> EmailNotification:
        self.db.flush()
        self.db.refresh(notification)
        return notification

    def delete_by_candidate(self, candidate_id: UUID) -> None:
        """
        Candidate erasure - removes this candidate's own email_notifications
        rows. Explicitly filtered on recipient_type = CANDIDATE as well as
        candidate_id, even though candidate_id is already NULL on every
        EXTERNAL_INTERVIEWER row today (the CHECK constraint enforces it) -
        relying on that NULL behavior incidentally, rather than stating the
        intent directly, is exactly the kind of thing that quietly turns
        into a real compliance bug the day this schema shifts again.
        """
        self.db.execute(
            delete(EmailNotification).where(
                EmailNotification.recipient_type == EmailRecipientType.CANDIDATE,
                EmailNotification.candidate_id == candidate_id,
            )
        )
        self.db.flush()

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
