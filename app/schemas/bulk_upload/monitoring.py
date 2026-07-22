from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.schemas.resume.monitoring import (
    CandidateSummary,
    EmbeddingStatus,
    FailureInfo,
    ParserInfo,
    ProcessingSummary,
    ResumeSummary,
    SkillSummary,
    StageTimelineBase,
)


class BulkFileListItem(BaseModel):
    id: UUID
    original_filename: str
    status: str
    task_id: str | None
    retry_count: int | None
    created_at: datetime


class BulkFileListResponse(BaseModel):
    items: list[BulkFileListItem]
    total: int
    page: int
    size: int


class BulkFileDetailResponse(BaseModel):
    file_id: UUID
    bulk_upload_job_id: UUID
    original_filename: str
    file_status: str
    task_id: str | None
    # Nullable per docs/Resume_Intake_Monitoring_API_Design.md #6 — a file
    # that failed before AI_EXTRACTION resolved an identity never gets a
    # Resume row at all (parse-first bulk architecture), so these — and
    # everything derived from a resume — stay null rather than erroring.
    resume: ResumeSummary | None
    candidate: CandidateSummary | None
    processing: ProcessingSummary
    skill_summary: SkillSummary | None
    embedding_status: EmbeddingStatus | None
    parser_info: ParserInfo | None
    failure: FailureInfo | None


class BulkFileTimelineResponse(StageTimelineBase):
    file_id: UUID


class BulkJobMetricsResponse(BaseModel):
    bulk_upload_job_id: UUID
    total_files: int
    processed: int
    failed: int
    duplicate: int
    avg_duration_by_stage: dict[str, float]
    retry_rate: float
    success_rate: float


class BulkJobFailureItem(BaseModel):
    file_id: UUID
    original_filename: str
    failed_stage: str | None
    error_message: str | None
    classification: str | None
    retry_count: int | None
    failed_at: datetime | None


class BulkJobFailureListResponse(BaseModel):
    items: list[BulkJobFailureItem]
    total: int
    page: int
    size: int
