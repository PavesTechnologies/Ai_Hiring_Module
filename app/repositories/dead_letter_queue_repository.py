from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models.async_tasks import DeadLetterQueue


class DeadLetterQueueRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_task_id(self, original_task_id: str) -> DeadLetterQueue | None:
        """Read-only — monitoring lookup, no writes."""
        stmt = select(DeadLetterQueue).where(DeadLetterQueue.original_task_id == original_task_id)
        return self.db.execute(stmt).scalars().first()

    def get_by_id(self, dlq_id: UUID) -> DeadLetterQueue | None:
        """Epic 4 (M05-E04) Phase D10 - keyed by the row's own PK, for individual-resume DLQ replay."""
        return self.db.get(DeadLetterQueue, dlq_id)

    def mark_replayed(self, dlq_id: UUID, replayed_by: str, replayed_at: datetime) -> None:
        self.db.execute(
            update(DeadLetterQueue)
            .where(DeadLetterQueue.id == dlq_id)
            .values(replayed_at=replayed_at, replayed_by=replayed_by)
        )
        self.db.flush()

    def create(
        self,
        *,
        original_task_id: str,
        task_type: str,
        final_error_message: str,
        full_error_trace: str | None = None,
        input_payload: dict[str, Any] | None = None,
        retry_count: int,
        first_attempted_at: datetime,
        last_attempted_at: datetime,
        resume_id: UUID | None = None,
        campaign_candidate_id: UUID | None = None,
    ) -> DeadLetterQueue:
        entry = DeadLetterQueue(
            original_task_id=original_task_id,
            task_type=task_type,
            resume_id=resume_id,
            campaign_candidate_id=campaign_candidate_id,
            final_error_message=final_error_message,
            full_error_trace=full_error_trace,
            input_payload=input_payload,
            retry_count=retry_count,
            first_attempted_at=first_attempted_at,
            last_attempted_at=last_attempted_at,
        )
        self.db.add(entry)
        self.db.flush()
        self.db.refresh(entry)
        return entry

    def delete_by_resume_id(self, resume_id: UUID) -> None:
        """
        Candidate erasure — must run before CeleryTaskLogRepository.delete_by_resume_id,
        since dead_letter_queue.original_task_id is a NOT NULL FK to
        celery_task_log.task_id and would otherwise block that delete.
        """
        self.db.execute(delete(DeadLetterQueue).where(DeadLetterQueue.resume_id == resume_id))
        self.db.flush()

    def delete_by_campaign_candidate_id(self, campaign_candidate_id: UUID) -> None:
        """Candidate erasure — same FK-ordering note as delete_by_resume_id."""
        self.db.execute(delete(DeadLetterQueue).where(DeadLetterQueue.campaign_candidate_id == campaign_candidate_id))
        self.db.flush()

    def delete_by_task_id(self, original_task_id: str) -> None:
        """Dead-letter cleanup — removes the dead-letter entry for one task_id."""
        self.db.execute(delete(DeadLetterQueue).where(DeadLetterQueue.original_task_id == original_task_id))
        self.db.flush()

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
