from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from app.models.candidates import ParseStatus
from app.models.pipeline import PipelineStage


class UploaderOption(BaseModel):
    """Epic 4 (M05-E04) Phase D7 - one entry in the uploaded_by filter dropdown."""

    user_id: str
    full_name: str | None = None


class UnifiedUploadHistoryEntry(BaseModel):
    """
    Epic 4 (M05-E04) Phase D7 - one row in the unified upload history,
    discriminated by upload_type. filename is always None for individual
    rows - Resume carries no stored original filename anywhere in this
    codebase (an honest, known gap, not a bug - see the plan/log for detail).
    """

    upload_type: Literal["individual", "bulk"]
    filename: str | None = None
    uploaded_by: str
    uploaded_by_name: str | None = None
    created_at: datetime
    # Normalized across ParseStatus/BulkUploadStatus - see
    # UploadHistoryService._derive_*_outcome for the exact mapping.
    outcome: str

    # Individual-only fields - always None on a "bulk" row.
    resume_id: UUID | None = None
    parse_status: ParseStatus | None = None
    pipeline_stage: PipelineStage | None = None

    # Bulk-only fields - always None on an "individual" row.
    bulk_upload_job_id: UUID | None = None
    total_files: int | None = None
    processed_count: int | None = None
    failed_count: int | None = None
    duplicate_count: int | None = None
    status: str | None = None


class UnifiedUploadHistoryResponse(BaseModel):
    entries: list[UnifiedUploadHistoryEntry]
    total: int
    limit: int
    offset: int
    # Derived from the already-fetched result set for this campaign - no
    # extra query. Every distinct uploader across both individual and bulk
    # rows, regardless of the currently-applied filters.
    available_uploaders: list[UploaderOption]
