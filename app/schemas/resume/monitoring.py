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
    dlq_id: UUID | None = None


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
    resume_id: UUID
    task_id: str | None
    candidate_id: UUID
    campaign_candidate_id: UUID | None
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


class ResumeListItemWithPipeline(ResumeListItem):
    """
    Same row shape as ResumeListItem, plus where this candidate's campaign
    pipeline currently stands - a separate endpoint/response rather than
    added fields on the base list, so existing ResumeListItem consumers are
    unaffected.
    """

    # Null whenever there's no linked campaign_candidate row (e.g. an
    # individually-uploaded resume not yet attached to a campaign).
    campaign_id: UUID | None
    # PipelineStage enum value (UPLOADED/SCREENING/.../SELECTED/REJECTED/
    # FRAUD_REVIEW) - what stage they're at right now.
    pipeline_stage: str | None
    # DecisionType enum value (REJECTED/SHORTLISTED/SELECTED/HOLD/
    # FRAUD_REVIEW/RESET) - the outcome of the most recent decision, i.e.
    # whether they succeeded or failed at their current/last stage. Null
    # until a decision has actually been made.
    decision_type: str | None
    # Which layer made that call - DETERMINISTIC/SEMANTIC/AI (the 3
    # automated screening layers) or RECRUITER/HIRING_MANAGER/HR_ADMIN/
    # SYSTEM for a manual one.
    decision_source: str | None
    # Human-readable reason - e.g. "Missing mandatory skill: Kubernetes"
    # for a DETERMINISTIC rejection. Null until a decision has been made.
    decision_reason: str | None
    decision_at: datetime | None


class ResumeListWithPipelineResponse(BaseModel):
    items: list[ResumeListItemWithPipeline]
    total: int
    page: int
    size: int


class ResumeParsedJsonResponse(BaseModel):
    resume_id: UUID
    candidate_id: UUID
    parse_status: str
    parsed_json: dict | None
    original_filename: str | None
    file_format: str
    file_size_bytes: int | None
    page_count: int | None
    created_at: datetime
    updated_at: datetime | None
    download_url: str | None
