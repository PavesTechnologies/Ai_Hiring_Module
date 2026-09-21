from uuid import UUID

from pydantic import BaseModel
from datetime import datetime


class GetJDResponse(BaseModel):
    id: UUID
    job_id: str
    title: str
    raw_text: str
    extracted_json: dict | None
    required_skills: dict | None
    min_experience_years: float | None
    max_experience_years: float | None
    notice_period: int | None
    education_criteria: dict | None
    source_format: str
    original_filename: str | None
    jurisdiction: str | None
    version_number: int
    is_active_version: bool
    is_verified: str
    created_by: str
    created_at: datetime
    updated_at: datetime | None
    prompt_template_id: UUID
    prompt_name: str | None

class UpdateJDResponse(BaseModel):
    id: UUID
    title: str
    version_number: int
    updated_by: str
    prompt_template_id: UUID
    prompt_name: str | None

class JDListItem(BaseModel):
    id: UUID
    job_id: str
    title: str
    version_number: int
    jurisdiction: str | None
    source_format: str
    is_verified: str
    is_active_version: bool
    active_campaigns_count: int
    passed_campaigns_count: int
    created_by: str
    created_at: datetime
    prompt_template_id: UUID
    prompt_name: str | None


class PaginatedJDResponse(BaseModel):
    total: int
    page: int
    size: int
    items: list[JDListItem]


class JDProcessingAcceptedResponse(BaseModel):
    task_id: UUID
    status: str


class StageProgress(BaseModel):
    stage: str
    status: str
    # User-facing copy (see error_presenter); error_detail keeps the raw
    # exception string for support. Both are None when the stage is fine.
    error_message: str | None
    error_detail: str | None = None
    duration_ms: int | None
    # Retry visibility. attempt_number is the *pipeline* attempt this row
    # was recorded on (not a per-stage counter) - on attempt 5 every
    # already-done stage is re-listed as SKIPPED with attempt_number=5.
    attempt_number: int
    max_attempts: int
    # Only meaningful on the stage that actually failed: that is the stage
    # whose policy decided the retry, so it is the only row where
    # "attempts left" answers a real question. None on SUCCESS/SKIPPED/
    # RUNNING rows rather than a number, because comparing a skipped
    # stage's pipeline-wide attempt_number against its own max_attempts
    # produces a confident-looking lie (TEXT_EXTRACTION SKIPPED on
    # attempt 5 vs. its ceiling of 3 = "0 left", when nothing about
    # TEXT_EXTRACTION was retried at all). Task-level retries_remaining
    # below is the authoritative number for the upload as a whole.
    retries_remaining: int | None


class JDProcessingStatusResponse(BaseModel):
    task_id: UUID
    overall_status: str
    current_stage: str | None
    stages: list[StageProgress]
    jd_id: UUID | None
    retry_count: int
    max_attempts: int
    retries_remaining: int
    error_message: str | None
    error_detail: str | None = None


class JDUploadStageProgress(BaseModel):
    """
    Upload history's stage row — the original four fields, unchanged.

    Kept separate from StageProgress (which the processing view uses)
    rather than reusing it: StageProgress gained attempt/retry fields for
    the processing tab, and adding those to this response would change
    what the existing upload-history UI receives.
    """

    stage: str
    status: str
    error_message: str | None
    duration_ms: int | None


class JDUploadSummary(BaseModel):
    task_id: UUID
    title: str | None
    status: str
    current_stage: str | None
    stages: list[JDUploadStageProgress]
    jd_id: UUID | None
    error_message: str | None
    queued_at: datetime