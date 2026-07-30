from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.schemas.campaign.campaign_processing_queue_response import (EstimatedCompletionResponse,
)


class ProcessingStatusSummaryResponse(BaseModel):
    """celery_task_log status breakdown for this campaign's tasks."""
    queued_count: int = 0
    running_count: int = 0
    retry_count: int = 0
    dead_count: int = 0
    paused_count: int = 0
    dead_letter_queue_count: int = 0
    # HR_ADMIN + RECRUITER both hit this endpoint, so the completion
    # estimate rides here too — RECRUITER gets it without gaining access to
    # the HR_ADMIN-only /processing-queue breakdown.
    estimated_completion: EstimatedCompletionResponse | None = None


class DeadLetterQueueEntryResponse(BaseModel):
    id: UUID
    task_type: str
    final_error_message: str
    retry_count: int
    moved_to_dlq_at: datetime
    campaign_candidate_id: UUID | None
    # additions:
    last_attempted_at: datetime | None = None
    resolution_notes: str | None = None
    replayed_at: datetime | None = None
    replay_supported: bool = False       # is this task_type in the replay registry?
