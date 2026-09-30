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
    candidate_name: str | None = None
    last_attempted_at: datetime | None = None
    resolution_notes: str | None = None
    replayed_at: datetime | None = None
    replay_supported: bool = False       # is this task_type in the replay registry?
    # Chain view - one entry per piece of work (task_type + candidate/resume);
    # this row is the chain's newest attempt, `history` its older ones.
    error_summary: str = ""              # short UI phrase; final_error_message stays the raw text
    status: str = "OPEN"                 # OPEN | REPLAYING | LIMIT_REACHED | RESOLVED
    attempt_count: int = 1               # dead-lettered attempts in this chain, this one included
    replays_used: int = 0
    replay_limit: int | None = None      # platform_config MAX_DLQ_REPLAYS_PER_TASK
    resolved_at: datetime | None = None
    history: list["DeadLetterQueueAttemptResponse"] = []


class DeadLetterQueueAttemptResponse(BaseModel):
    """An older, already-replayed attempt in a DLQ chain - kept for audit."""
    id: UUID
    error_summary: str
    moved_to_dlq_at: datetime
    replayed_at: datetime | None = None


DeadLetterQueueEntryResponse.model_rebuild()


class DeadLetterQueuePageResponse(BaseModel):
    """
    Paginated, replayable-only DLQ listing for a campaign - one entry per
    chain, open chains only unless include_resolved.
    """
    entries: list[DeadLetterQueueEntryResponse]
    total: int
    limit: int
    offset: int
