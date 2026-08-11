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


class ResumeRetryResponse(BaseModel):
    """Epic 4 (M05-E04) Phase D10 - response for both retry_parse and replay_from_dlq."""

    resume_id: UUID
    task_id: UUID
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


class ResumeVersionCampaignUsage(BaseModel):
    campaign_id: UUID
    campaign_name: str
    pipeline_stage: str


class ResumeVersionItem(BaseModel):
    id: UUID
    version_number: int
    is_active_version: bool
    file_format: str
    parse_status: str
    parse_confidence: float | None
    uploaded_by: str
    source: str  # "individual" | "bulk"
    created_at: datetime
    campaigns: list[ResumeVersionCampaignUsage]


class ResumeVersionHistoryResponse(BaseModel):
    candidate_id: UUID
    versions: list[ResumeVersionItem]


class ResumeDownloadUrlResponse(BaseModel):
    resume_id: UUID
    version_number: int
    download_url: str
    expires_in_seconds: int


# ----------------------------------------------------------------------
# S02-T02 - Compare Resume Versions. Computed entirely at query time from
# the two resumes' current parsed_json; never persisted.
# ----------------------------------------------------------------------

class SkillsComparison(BaseModel):
    added: list[str]
    removed: list[str]
    unchanged: list[str]


class ExperienceEntryComparison(BaseModel):
    title: str | None
    company: str | None
    start_date: str | None
    end_date: str | None
    is_current: bool


class ExperienceComparison(BaseModel):
    added: list[ExperienceEntryComparison]
    removed: list[ExperienceEntryComparison]


class EducationEntryComparison(BaseModel):
    degree: str | None
    institution: str | None
    field: str | None
    graduation_year: int | None


class EducationComparison(BaseModel):
    added: list[EducationEntryComparison]
    removed: list[EducationEntryComparison]


class ExperienceYearsComparison(BaseModel):
    version_1: float | None
    version_2: float | None
    difference: float | None


class ResumeComparisonSummary(BaseModel):
    skills_added: int
    skills_removed: int
    skills_unchanged: int
    experience_years_change: float | None


class ResumeVersionSnapshot(BaseModel):
    """Full raw parsed_json alongside version metadata, so a caller can render both versions side by side."""
    resume_id: UUID
    version_number: int
    parse_status: str
    created_at: datetime
    parsed_json: dict


class ResumeVersionComparisonResponse(BaseModel):
    candidate_id: UUID
    version_1: ResumeVersionSnapshot
    version_2: ResumeVersionSnapshot
    skills: SkillsComparison
    experience: ExperienceComparison
    education: EducationComparison
    experience_years: ExperienceYearsComparison
    summary: ResumeComparisonSummary
