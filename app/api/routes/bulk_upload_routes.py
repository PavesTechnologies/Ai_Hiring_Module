from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Security, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from app.dependencies.bulk_upload import get_bulk_upload_service
from app.enums.constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, UserRole
from app.exception_handler.exceptions import BadRequestError
from app.middleware.rbac import TokenUser, require_roles
from app.schemas.bulk_upload.request import BulkUploadRequest
from app.schemas.bulk_upload.response import (
    BulkUploadAcceptedResponse,
    BulkUploadCancelResponse,
    BulkUploadHistoryListResponse,
    BulkUploadJobDetailResponse,
    BulkUploadJobFileItem,
    BulkUploadJobSummary,
)
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


@router.get(
    "",
    response_model=APIResponse[BulkUploadHistoryListResponse],
    status_code=status.HTTP_200_OK,
)
def list_bulk_upload_history(
    campaign_id: UUID = Query(...),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    service: BulkUploadService = Depends(get_bulk_upload_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    """Paginated bulk-upload history for one campaign, most recent first."""
    items, total = service.list_history(campaign_id=campaign_id, page=page, size=size)

    return APIResponse.ok(
        data=BulkUploadHistoryListResponse(
            total=total,
            page=page,
            size=size,
            items=[
                BulkUploadJobSummary(
                    id=job.id,
                    original_filename=job.original_filename,
                    status=job.status.value,
                    total_files=job.total_files,
                    processed_count=job.processed_count,
                    failed_count=job.failed_count,
                    duplicate_count=job.duplicate_count,
                    created_at=job.created_at,
                    completed_at=job.completed_at,
                )
                for job in items
            ],
        ),
        message="Bulk upload history retrieved successfully.",
    )


@router.get(
    "/export",
    status_code=status.HTTP_200_OK,
)
def export_bulk_upload_history(
    campaign_id: UUID = Query(...),
    service: BulkUploadService = Depends(get_bulk_upload_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    """
    Excel export of a campaign's full bulk-upload history (unpaginated).
    Registered before /{bulk_upload_job_id} so "export" isn't swallowed as
    a job id path parameter — mirrors jd_routes.py's export/{jd_id} ordering.
    """
    excel_file = service.export_history(
        campaign_id=campaign_id,
        actor_id=user.user_id,
        actor_role=user.roles[0] if user.roles else None,
    )

    filename = f"Bulk_Upload_History_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

    return StreamingResponse(
        excel_file,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/{bulk_upload_job_id}",
    response_model=APIResponse[BulkUploadJobDetailResponse],
    status_code=status.HTTP_200_OK,
)
def get_bulk_upload_detail(
    bulk_upload_job_id: UUID,
    service: BulkUploadService = Depends(get_bulk_upload_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    """One bulk upload job's full detail, including its per-file breakdown."""
    job, files = service.get_job_detail(bulk_upload_job_id)

    return APIResponse.ok(
        data=BulkUploadJobDetailResponse(
            id=job.id,
            campaign_id=job.campaign_id,
            uploaded_by=job.uploaded_by,
            original_filename=job.original_filename,
            status=job.status.value,
            consent_confirmed=job.consent_confirmed,
            total_files=job.total_files,
            queued_count=job.queued_count,
            processed_count=job.processed_count,
            failed_count=job.failed_count,
            duplicate_count=job.duplicate_count,
            error_summary=job.error_summary,
            created_at=job.created_at,
            completed_at=job.completed_at,
            files=[
                BulkUploadJobFileItem(
                    id=f.id,
                    original_filename=f.original_filename,
                    status=f.status.value,
                )
                for f in files
            ],
        ),
        message="Bulk upload detail retrieved successfully.",
    )


@router.post(
    "/{bulk_upload_job_id}/cancel",
    response_model=APIResponse[BulkUploadCancelResponse],
    status_code=status.HTTP_200_OK,
)
def cancel_bulk_upload(
    bulk_upload_job_id: UUID,
    service: BulkUploadService = Depends(get_bulk_upload_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    """
    Cancels a bulk upload that hasn't finished yet: the job moves to
    CANCELLED and every still-queued file is bulk-marked CANCELLED. Any
    file whose per-file task is already running is left to finish
    naturally — this mirrors the campaign-pause behavior, since no
    real Celery-level task revocation exists in this codebase.
    """
    job, files_cancelled = service.cancel_job(
        job_id=bulk_upload_job_id,
        actor_id=user.user_id,
        actor_role=user.roles[0] if user.roles else None,
    )

    return APIResponse.ok(
        data=BulkUploadCancelResponse(
            bulk_upload_job_id=job.id,
            status=job.status.value,
            files_cancelled=files_cancelled,
        ),
        message="Bulk upload cancelled.",
    )
