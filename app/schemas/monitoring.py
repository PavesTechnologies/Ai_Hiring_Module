from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class QueueStatusResponse(BaseModel):
    resumes_queued: int
    resumes_running: int
    bulk_files_queued: int
    bulk_files_running: int


class FailureReasonItem(BaseModel):
    exception_type: str
    count: int


class ProcessingMetricsResponse(BaseModel):
    window: str
    throughput_per_hour: float
    avg_duration_by_stage: dict[str, float]
    failure_rate_by_stage: dict[str, float]
    top_failure_reasons: list[FailureReasonItem]


class CircuitBreakerStatusItem(BaseModel):
    service_name: str
    state: str
    failure_count: int
    opened_at: datetime | None = None


class CampaignQueueBreakdownItem(BaseModel):
    campaign_id: UUID
    campaign_name: str
    pending_resumes_count: int
    queued_resumes_count: int
    queue_depth: int


class UploadQueueDashboardResponse(BaseModel):
    """Epic 4 (M05-E04) Phase D12 - platform-wide upload queue dashboard, HR_ADMIN-only."""

    generated_at: datetime
    pending_resumes_count: int
    resumes_queued: int
    resumes_running: int
    processing_bulk_jobs_count: int
    dead_tasks_count: int
    circuit_breakers: list[CircuitBreakerStatusItem]
    any_circuit_breaker_open: bool
    campaign_breakdown: list[CampaignQueueBreakdownItem]
