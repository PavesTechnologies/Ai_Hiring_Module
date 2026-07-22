from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class StageExecutionDetail(BaseModel):
    stage: str
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None
    attempt_number: int
    error_message: str | None
    skipped: bool
    retryable: bool | None


class StageTimelineBase(BaseModel):
    task_id: str
    document_type: str
    overall_status: str
    current_stage: str | None
    attempt_number: int
    retry_count: int
    progress_percent: float
    queued_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    stages: list[StageExecutionDetail]


class ResumeTimelineResponse(StageTimelineBase):
    resume_id: UUID


class ParseAttemptItem(BaseModel):
    source: str  # "parse_attempt" (resume_parse_attempts) | "stage_failure" (stage_failure_logs)
    attempt_number: int
    stage: str | None
    parser_used: str | None
    parser_version: str | None
    status: str
    error_code: str | None
    error_detail: str | None
    confidence_score: float | None
    duration_ms: int | None
    occurred_at: datetime


class ResumeSummary(BaseModel):
    id: UUID
    file_path: str
    file_format: str
    version_number: int
    is_active_version: bool
    parse_status: str
    parser_version: str | None
    page_count: int | None
    created_at: datetime
    bulk_upload_job_id: UUID | None


class CandidateSummary(BaseModel):
    id: UUID
    full_name: str
    email: str
    jurisdiction: str
    consent_given: bool


class ProcessingSummary(BaseModel):
    task_id: str | None
    current_status: str | None
    current_stage: str | None
    attempt_number: int | None
    retry_count: int | None


class SkillSummary(BaseModel):
    total_skills: int
    matched: int
    unmatched: int
    by_tier: dict[str, int]


class EmbeddingStatus(BaseModel):
    exists: bool
    embedding_model_version_id: UUID | None
    generated_at: datetime | None


class ParserInfo(BaseModel):
    parser_used: str | None
    parser_version: str | None


class FailureInfo(BaseModel):
    failed_stage: str | None
    error_message: str | None
    classification: str | None
    moved_to_dlq: bool


class ResumeDetailResponse(BaseModel):
    resume: ResumeSummary
    candidate: CandidateSummary
    processing: ProcessingSummary
    skill_summary: SkillSummary
    embedding_status: EmbeddingStatus
    parser_info: ParserInfo
    failure: FailureInfo | None


class ResumeListItem(BaseModel):
    id: UUID
    candidate_id: UUID
    candidate_full_name: str
    candidate_email: str
    file_format: str
    parse_status: str
    version_number: int
    is_active_version: bool
    source: str  # "individual" | "bulk"
    bulk_upload_job_id: UUID | None
    created_at: datetime


class ResumeListResponse(BaseModel):
    items: list[ResumeListItem]
    total: int
    page: int
    size: int
