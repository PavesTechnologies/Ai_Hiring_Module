from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.interview import InterviewInterviewer, InterviewSchedule, InterviewScheduleHistory


class InterviewScheduleRepository:

    def __init__(self, db: Session):
        self.db = db

    def get_by_campaign_candidate_id(self, campaign_candidate_id: UUID) -> InterviewSchedule | None:
        stmt = select(InterviewSchedule).where(
            InterviewSchedule.campaign_candidate_id == campaign_candidate_id,
        )
        return self.db.execute(stmt).scalars().first()

    def get_by_id(self, interview_id: UUID) -> InterviewSchedule | None:
        return self.db.get(InterviewSchedule, interview_id)

    def get_or_create_pending(self, campaign_candidate_id: UUID) -> tuple[InterviewSchedule, bool]:
        """
        Epic 4: StageTransitionService.transition()'s INTERVIEW-entry hook -
        check-then-create, not a SAVEPOINT/IntegrityError-catch idempotent
        insert like campaign_candidate_repository.create_idempotent(). That
        pattern exists to survive a genuine race between two independent
        writers; there isn't one here - transition() already holds a
        FOR UPDATE lock on the campaign_candidate row before this ever runs,
        so two calls for the same campaign_candidate_id can never execute
        this concurrently. A plain check-then-create is both sufficient and
        simpler under that guarantee.

        Returns (row, was_created) - was_created=False on a re-entry into
        INTERVIEW (e.g. after a fraud-review clear) that already has a row
        from its first entry; the existing row is returned untouched, never
        reset back to PENDING.
        """
        existing = self.get_by_campaign_candidate_id(campaign_candidate_id)
        if existing is not None:
            return existing, False

        schedule = InterviewSchedule(campaign_candidate_id=campaign_candidate_id)
        self.db.add(schedule)
        self.db.flush()
        self.db.refresh(schedule)
        return schedule, True

    def update(self, schedule: InterviewSchedule) -> InterviewSchedule:
        self.db.flush()
        self.db.refresh(schedule)
        return schedule

    def get_interviewers(self, interview_id: UUID) -> list[InterviewInterviewer]:
        stmt = select(InterviewInterviewer).where(InterviewInterviewer.interview_id == interview_id)
        return list(self.db.execute(stmt).scalars().all())

    def replace_interviewers(
        self, interview_id: UUID, interviewers: list[dict],
    ) -> list[InterviewInterviewer]:
        """
        Step 3: schedule/reschedule both send a full interviewers list each
        time (not a delta) - delete-then-recreate is the whole set's source
        of truth, matching how the wire contract describes it, rather than
        diffing against whatever rows already exist.
        """
        self.db.execute(delete(InterviewInterviewer).where(InterviewInterviewer.interview_id == interview_id))
        rows = [
            InterviewInterviewer(interview_id=interview_id, name=i["name"], email=i["email"])
            for i in interviewers
        ]
        self.db.add_all(rows)
        self.db.flush()
        for row in rows:
            self.db.refresh(row)
        return rows

    def add_history(
        self,
        *,
        interview_id: UUID,
        event_type,
        old_start_at,
        new_start_at,
        changed_by: str,
        changed_by_role: str | None,
        reason: str | None,
    ) -> InterviewScheduleHistory:
        entry = InterviewScheduleHistory(
            interview_id=interview_id,
            event_type=event_type,
            old_start_at=old_start_at,
            new_start_at=new_start_at,
            changed_by=changed_by,
            changed_by_role=changed_by_role,
            reason=reason,
        )
        self.db.add(entry)
        self.db.flush()
        self.db.refresh(entry)
        return entry

    def get_history(self, interview_id: UUID) -> list[InterviewScheduleHistory]:
        stmt = (
            select(InterviewScheduleHistory)
            .where(InterviewScheduleHistory.interview_id == interview_id)
            .order_by(InterviewScheduleHistory.changed_at.asc())
        )
        return list(self.db.execute(stmt).scalars().all())

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
