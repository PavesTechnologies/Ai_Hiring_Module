import enum
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class BulkUploadAcceptedResponse(BaseModel):
    bulk_upload_job_id: UUID
    task_id: UUID
    campaign_name: str
    original_filename: str
    status: str


class BulkUploadCancelResponse(BaseModel):
    bulk_upload_job_id: UUID
    status: str
    files_cancelled: int


class BulkUploadProgressResponse(BaseModel):
    """
    Epic 4 (M05-E04) Phase D4 — lightweight, dedicated polling response.
    Deliberately separate from BulkUploadJobDetailResponse, which also
    embeds the full per-file list (unsuited to a frequent 10s poll).
    """
    bulk_upload_job_id: UUID
    status: str
    total_files: int
    processed_count: int
    failed_count: int
    duplicate_count: int
    remaining_count: int
    # Clamped to 100.0 - a guard against counter inconsistencies, never
    # itself the source of truth (the raw counters above always are).
    percent_complete: float
    # Linear estimate only, not a model - None unless status=PROCESSING
    # and at least one file has resolved so far.
    estimated_completion_at: datetime | None


class BulkUploadFileLogResult(str, enum.Enum):
    """Epic 4 (M05-E04) Phase D5 - the 4 result badges the live file log surfaces."""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    DUPLICATE = "DUPLICATE"
    SKIPPED = "SKIPPED"


class BulkUploadFileLogEntry(BaseModel):
    filename: str
    result: BulkUploadFileLogResult
    # Always populated for FAILED/SKIPPED, always None for SUCCESS/DUPLICATE.
    reason: str | None = None
    timestamp: datetime


class BulkUploadFileLogResponse(BaseModel):
    entries: list[BulkUploadFileLogEntry]
    total: int
    limit: int
    offset: int


class BulkUploadJobSummary(BaseModel):
    id: UUID
    original_filename: str
    status: str
    total_files: int
    queued_count: int
    processed_count: int
    failed_count: int
    duplicate_count: int
    created_at: datetime
    completed_at: datetime | None
    # M04-E04-S02-T01: shown when expanding a FAILED/PARTIAL_FAILURE job in
    # the campaign detail page's Bulk Uploads section.
    error_summary: str | None = None


class BulkUploadHistoryListResponse(BaseModel):
    total: int
    page: int
    size: int
    items: list[BulkUploadJobSummary]


class BulkUploadJobFileItem(BaseModel):
    id: UUID
    original_filename: str
    status: str
    # Correlates this file to its own celery_task_log row - null until the
    # per-file parse task has actually been enqueued (e.g. still QUEUED
    # before task dispatch, or a file that never got that far).
    task_id: str | None = None
    retry_count: int | None = None


class BulkUploadFileReplayResponse(BaseModel):
    file_id: UUID
    bulk_upload_job_id: UUID
    original_filename: str
    status: str
    new_task_id: str


class BulkUploadJobDetailResponse(BaseModel):
    id: UUID
    campaign_id: UUID
    uploaded_by: str
    original_filename: str
    status: str
    consent_confirmed: bool
    total_files: int
    queued_count: int
    processed_count: int
    failed_count: int
    duplicate_count: int
    error_summary: str | None
    created_at: datetime
    completed_at: datetime | None
    files: list[BulkUploadJobFileItem]
