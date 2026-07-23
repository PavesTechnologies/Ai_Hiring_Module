from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.async_tasks import DeadLetterQueue


class DeadLetterQueueRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_task_id(self, original_task_id: str) -> DeadLetterQueue | None:
        """Read-only — monitoring lookup, no writes."""
        stmt = select(DeadLetterQueue).where(DeadLetterQueue.original_task_id == original_task_id)
        return self.db.execute(stmt).scalars().first()

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

    def commit(self) -> None:
        self.db.commit()
