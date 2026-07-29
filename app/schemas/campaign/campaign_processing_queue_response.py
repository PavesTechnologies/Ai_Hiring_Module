from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class TaskTypeBreakdownResponse(BaseModel):
    """one row per real task_type present for this campaign."""

    task_type: str
    status_counts: dict[str, int]        # e.g. {"QUEUED": 3, "SUCCESS": 41, "DEAD": 1}
    avg_duration_ms: float | None        # avg over SUCCESS rows; None if none completed
    total_token_count: int               # cumulative LLM token usage (0 for non-LLM types)


class CircuitBreakerSummaryResponse(BaseModel):
    service_name: str
    state: str                           # CLOSED / OPEN / HALF_OPEN
    failure_count: int
    opened_at: datetime | None
    retry_after: datetime | None


class EstimatedCompletionResponse(BaseModel):
    """human-facing completion estimate."""

    remaining_task_count: int
    estimate_available: bool
    # set when estimate_available:
    min_minutes: int | None = None
    max_minutes: int | None = None
    # always set — the exact string the UI should show:
    message: str


class ProcessingQueueResponse(BaseModel):
    """the full Processing Queue section payload (HR_ADMIN)."""

    task_types: list[TaskTypeBreakdownResponse]
    circuit_breakers: list[CircuitBreakerSummaryResponse]
    estimated_completion: EstimatedCompletionResponse


class DLQReplayRequest(BaseModel):
    dlq_ids: list[UUID]


class DLQReplayResultItem(BaseModel):
    dlq_id: UUID
    status: str                          # "REPLAYED" | "SKIPPED"
    reason: str | None = None
    new_task_id: str | None = None


class DLQReplayResponse(BaseModel):
    replayed_count: int
    skipped_count: int
    results: list[DLQReplayResultItem]
