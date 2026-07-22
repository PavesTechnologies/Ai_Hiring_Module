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
