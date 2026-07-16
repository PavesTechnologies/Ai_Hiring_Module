from uuid import UUID

from pydantic import BaseModel


class BulkUploadAcceptedResponse(BaseModel):
    bulk_upload_job_id: UUID
    task_id: UUID
    campaign_name: str
    original_filename: str
    status: str
