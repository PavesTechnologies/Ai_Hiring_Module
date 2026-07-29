from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class ResumeUploadAcceptedResponse(BaseModel):
    resume_id: UUID
    campaign_candidate_id: UUID
    # Epic 3 (M05-E03) Phase C2 — optional because a "use_existing"
    # duplicate resolution links a resume that may predate task_id tracking
    # (or was itself never (re)processed), and no new task is enqueued.
    task_id: UUID | None
    candidate_name_masked: str
    file_name: str
    campaign_name: str
    pipeline_stage: str
    parse_status: str


class DuplicateFileWarningResponse(BaseModel):
    duplicate_resume_id: UUID
    candidate_id: UUID
    candidate_name: str
    uploaded_at: datetime
    current_pipeline_stage: str | None
    campaign_names: list[str]
    # Never fabricated: individual uploads have no stored original filename
    # at all (Resume carries no such column) — always null for them.
    original_filename: str | None
    available_resolutions: list[str]


class StageProgress(BaseModel):
    stage: str
    status: str
    error_message: str | None
    duration_ms: int | None


class ResumeProcessingStatusResponse(BaseModel):
    task_id: UUID
    overall_status: str
    current_stage: str | None
    stages: list[StageProgress]
    resume_id: UUID | None
    error_message: str | None


class ResumeVersionItem(BaseModel):
    id: UUID
    version_number: int
    is_active_version: bool
    file_format: str
    parse_status: str
    source: str  # "individual" | "bulk"
    created_at: datetime


class ResumeVersionHistoryResponse(BaseModel):
    candidate_id: UUID
    versions: list[ResumeVersionItem]
