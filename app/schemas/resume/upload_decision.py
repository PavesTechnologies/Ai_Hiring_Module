from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

DUPLICATE_FILE_IN_CAMPAIGN = "DUPLICATE_FILE_IN_CAMPAIGN"
CANDIDATE_IN_CAMPAIGN_NEW_FILE = "CANDIDATE_IN_CAMPAIGN_NEW_FILE"
NEW_FILE_RESOLUTIONS = ["keep_existing", "use_new_version"]


class DuplicateFileInCampaignResponse(BaseModel):
    """`data` of the 409 for an identical file already in this campaign (nothing was stored)."""
    error_code: str = DUPLICATE_FILE_IN_CAMPAIGN
    campaign_candidate_id: UUID
    candidate_id: UUID
    candidate_name: str | None
    current_pipeline_stage: str
    existing_resume_id: UUID
    existing_original_filename: str | None
    existing_uploaded_at: datetime


class CandidateInCampaignNewFileResponse(BaseModel):
    """
    `data` of the 409 for a candidate already in this campaign uploaded with a
    different file. Nothing was stored; resend the same upload with
    resolution=keep_existing or resolution=use_new_version.
    """
    error_code: str = CANDIDATE_IN_CAMPAIGN_NEW_FILE
    campaign_candidate_id: UUID
    candidate_id: UUID
    candidate_name: str | None
    current_pipeline_stage: str
    existing_resume_id: UUID
    existing_original_filename: str | None
    existing_uploaded_at: datetime | None
    existing_version_number: int | None
    available_resolutions: list[str]


class UploadDecisionItem(BaseModel):
    id: UUID
    campaign_id: UUID
    bulk_upload_job_id: UUID | None
    bulk_upload_job_file_id: UUID | None
    campaign_candidate_id: UUID | None
    candidate_id: UUID | None
    # Full name as on the scorecard / duplicate warning; email masked as in
    # the talent pool (j***n@example.com).
    candidate_name: str | None
    candidate_email_masked: str | None
    existing_resume_id: UUID | None
    existing_original_filename: str | None
    existing_uploaded_at: datetime | None
    new_original_filename: str | None
    uploaded_by: str
    created_at: datetime
    expires_at: datetime
    # PENDING | KEPT_EXISTING | NEW_VERSION_USED | EXPIRED_KEPT_EXISTING
    status: str
    decided_by: str | None = None
    decided_at: datetime | None = None
    new_resume_id: UUID | None = None


class UploadDecisionListResponse(BaseModel):
    items: list[UploadDecisionItem]
    total: int


class UploadDecisionDecideRequest(BaseModel):
    choice: Literal["keep_existing", "use_new_version"]


class UploadDecisionDecideResponse(BaseModel):
    decision: UploadDecisionItem
    # Only for use_new_version: the new resume version and its parse task.
    new_resume_id: UUID | None = None
    task_id: UUID | None = None
