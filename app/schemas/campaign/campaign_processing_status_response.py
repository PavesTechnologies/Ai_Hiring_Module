from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class ProcessingStatusSummaryResponse(BaseModel):
    """S01-T02: celery_task_log status breakdown for this campaign's tasks."""
    queued_count: int = 0
    running_count: int = 0
    retry_count: int = 0
    dead_count: int = 0
    paused_count: int = 0
    dead_letter_queue_count: int = 0


class DeadLetterQueueEntryResponse(BaseModel):
    id: UUID
    task_type: str
    final_error_message: str
    retry_count: int
    moved_to_dlq_at: datetime
    campaign_candidate_id: UUID | None
