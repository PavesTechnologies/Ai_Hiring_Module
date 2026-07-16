from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Security, UploadFile, status
from pydantic import ValidationError

from app.dependencies.bulk_upload import get_bulk_upload_service
from app.enums.constants import UserRole
from app.exception_handler.exceptions import BadRequestError
from app.middleware.rbac import TokenUser, require_roles
from app.schemas.bulk_upload.request import BulkUploadRequest
from app.schemas.bulk_upload.response import BulkUploadAcceptedResponse
from app.schemas.response import APIResponse
from app.services.bulk_upload.bulk_upload_service import BulkUploadService

router = APIRouter(
    prefix="/bulk-uploads",
    tags=["Bulk Resume Upload"],
)


@router.post(
    "",
    response_model=APIResponse[BulkUploadAcceptedResponse],
    status_code=status.HTTP_201_CREATED,
)
def upload_bulk_zip(
    campaign_id: UUID = Form(...),
    consent_confirmed: bool = Form(...),
    file: UploadFile = File(...),
    service: BulkUploadService = Depends(get_bulk_upload_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    """
    Validates and stores the ZIP archive, creates the bulk_upload_jobs
    record at status=PENDING, and enqueues the BULK_EXTRACT task, which
    unpacks the archive asynchronously. Per-file parsing is a later phase.
    """
    try:
        validated = BulkUploadRequest(
            campaign_id=campaign_id,
            consent_confirmed=consent_confirmed,
        )
    except ValidationError as exc:
        raise BadRequestError(str(exc)) from exc

    file_bytes = file.file.read()
    filename = file.filename or "bulk_upload.zip"

    job, campaign, task_id = service.upload_zip(
        campaign_id=validated.campaign_id,
        file_bytes=file_bytes,
        filename=filename,
        uploaded_by=user.user_id,
        consent_confirmed=validated.consent_confirmed,
    )

    return APIResponse.ok(
        data=BulkUploadAcceptedResponse(
            bulk_upload_job_id=job.id,
            task_id=task_id,
            campaign_name=campaign.name,
            original_filename=job.original_filename,
            status=job.status.value,
        ),
        message="Bulk upload received — processing will begin shortly.",
    )
