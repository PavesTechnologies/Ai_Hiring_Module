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


class BulkUploadJobSummary(BaseModel):
    id: UUID
    original_filename: str
    status: str
    total_files: int
    processed_count: int
    failed_count: int
    duplicate_count: int
    created_at: datetime
    completed_at: datetime | None


class BulkUploadHistoryListResponse(BaseModel):
    total: int
    page: int
    size: int
    items: list[BulkUploadJobSummary]


class BulkUploadJobFileItem(BaseModel):
    id: UUID
    original_filename: str
    status: str


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
