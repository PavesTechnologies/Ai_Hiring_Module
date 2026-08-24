from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.interview import InterviewFeedback, InterviewFeedbackRecommendation


class InterviewFeedbackRepository:

    def __init__(self, db: Session):
        self.db = db

    def get_by_interview_schedule_id(self, interview_schedule_id: UUID) -> list[InterviewFeedback]:
        stmt = (
            select(InterviewFeedback)
            .where(InterviewFeedback.interview_schedule_id == interview_schedule_id)
            .order_by(InterviewFeedback.submitted_at.asc())
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_by_interview_schedule_id_and_interviewer_id(
        self, interview_schedule_id: UUID, interviewer_id: UUID,
    ) -> InterviewFeedback | None:
        """
        Fix: backs the GET form-context endpoint's "already submitted"
        check - the same (interview_schedule_id, interviewer_id) pair
        create()'s UNIQUE constraint hard-locks against a second row.
        """
        stmt = select(InterviewFeedback).where(
            InterviewFeedback.interview_schedule_id == interview_schedule_id,
            InterviewFeedback.interviewer_id == interviewer_id,
        )
        return self.db.execute(stmt).scalars().first()

    def create(
        self,
        interview_schedule_id: UUID,
        interviewer_id: UUID,
        recommendation: InterviewFeedbackRecommendation,
        notes: str | None,
    ) -> tuple[InterviewFeedback, bool]:
        """
        SAVEPOINT + IntegrityError-catch (same shape as
        campaign_candidate_repository.create_idempotent() and
        InterviewScheduleRepository.create_next_round) - but unlike both
        of those, a loss here is NOT resolved by silently returning the
        existing row as a success. UNIQUE(interview_schedule_id,
        interviewer_id) is a deliberate hard lock: every other append-only
        guarantee in this system exists so a submitted decision is never
        silently overwritten, and feedback isn't the exception. The
        caller (InterviewFeedbackService) turns was_created=False into a
        409, not a quiet "here's what's already there."
        """
        feedback = InterviewFeedback(
            interview_schedule_id=interview_schedule_id,
            interviewer_id=interviewer_id,
            recommendation=recommendation,
            notes=notes,
        )
        try:
            with self.db.begin_nested():
                self.db.add(feedback)
                self.db.flush()
        except IntegrityError:
            existing = self.db.execute(
                select(InterviewFeedback).where(
                    InterviewFeedback.interview_schedule_id == interview_schedule_id,
                    InterviewFeedback.interviewer_id == interviewer_id,
                )
            ).scalars().first()
            return existing, False

        self.db.refresh(feedback)
        return feedback, True

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
