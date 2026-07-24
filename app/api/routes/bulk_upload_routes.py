from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Security, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from app.dependencies.bulk_upload import get_bulk_upload_monitoring_service, get_bulk_upload_service
from app.enums.constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, UserRole
from app.exception_handler.exceptions import BadRequestError
from app.middleware.rbac import TokenUser, require_roles
from app.models.async_tasks import BulkUploadFileStatus
from app.schemas.bulk_upload.monitoring import (
    BulkFileDetailResponse,
    BulkFileListResponse,
    BulkFileTimelineResponse,
    BulkJobFailureListResponse,
    BulkJobMetricsResponse,
)
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
from app.services.bulk_upload.bulk_upload_monitoring_service import BulkUploadMonitoringService
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
    job, files, retry_counts = service.get_job_detail(bulk_upload_job_id)

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
                    retry_count=retry_counts.get(f.task_id),
                )
                for f in files
            ],
        ),
        message="Bulk upload detail retrieved successfully.",
    )


@router.get(
    "/{bulk_upload_job_id}/files",
    response_model=APIResponse[BulkFileListResponse],
    status_code=status.HTTP_200_OK,
)
def list_bulk_upload_files(
    bulk_upload_job_id: UUID,
    status_filter: BulkUploadFileStatus | None = Query(default=None, alias="status"),
    search: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    sort_by: str = Query(default="created_at"),
    sort_dir: str = Query(default="desc"),
    service: BulkUploadMonitoringService = Depends(get_bulk_upload_monitoring_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    """
    Read-only monitoring endpoint — paginated, filterable, searchable file
    list for a bulk upload job. The embedded file array on
    GET /bulk-uploads/{id} stays as-is and unpaginated; this is what a UI
    should page through for a large ZIP.
    """
    return APIResponse.ok(
        data=service.list_files(
            bulk_upload_job_id,
            status=status_filter,
            search=search,
            page=page,
            size=size,
            sort_by=sort_by,
            sort_dir=sort_dir,
        ),
        message="Bulk upload files retrieved successfully.",
    )


@router.get(
    "/{bulk_upload_job_id}/files/{file_id}",
    response_model=APIResponse[BulkFileDetailResponse],
    status_code=status.HTTP_200_OK,
)
def get_bulk_upload_file_detail(
    bulk_upload_job_id: UUID,
    file_id: UUID,
    service: BulkUploadMonitoringService = Depends(get_bulk_upload_monitoring_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    """
    Read-only monitoring endpoint — mirrors the Resume Detail shape, with
    resume/candidate left null until a file's identity resolves (a file
    that fails before AI_EXTRACTION never gets a Resume row at all).
    """
    return APIResponse.ok(
        data=service.get_file_detail(bulk_upload_job_id, file_id),
        message="Bulk upload file detail retrieved successfully.",
    )


@router.get(
    "/{bulk_upload_job_id}/files/{file_id}/timeline",
    response_model=APIResponse[BulkFileTimelineResponse],
    status_code=status.HTTP_200_OK,
)
def get_bulk_upload_file_timeline(
    bulk_upload_job_id: UUID,
    file_id: UUID,
    attempt_number: int | None = Query(default=None, ge=1),
    service: BulkUploadMonitoringService = Depends(get_bulk_upload_monitoring_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    """
    Read-only monitoring endpoint — identical StageTimeline shape as
    GET /resumes/{id}/timeline. bulk_upload_job_files.task_id is populated
    at row-creation time (not just on success), so this resolves reliably
    at every point in a file's lifecycle.
    """
    return APIResponse.ok(
        data=service.get_file_timeline(bulk_upload_job_id, file_id, attempt_number=attempt_number),
        message="Bulk upload file timeline retrieved successfully.",
    )


@router.get(
    "/{bulk_upload_job_id}/metrics",
    response_model=APIResponse[BulkJobMetricsResponse],
    status_code=status.HTTP_200_OK,
)
def get_bulk_upload_job_metrics(
    bulk_upload_job_id: UUID,
    service: BulkUploadMonitoringService = Depends(get_bulk_upload_monitoring_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    """
    Read-only monitoring endpoint — total_files/processed/failed/duplicate
    come from the job's own maintained counters; avg_duration_by_stage and
    retry_rate are computed live over this job's files' stage executions
    and task logs.
    """
    return APIResponse.ok(
        data=service.get_job_metrics(bulk_upload_job_id),
        message="Bulk upload job metrics retrieved successfully.",
    )


@router.get(
    "/{bulk_upload_job_id}/failures",
    response_model=APIResponse[BulkJobFailureListResponse],
    status_code=status.HTTP_200_OK,
)
def get_bulk_upload_job_failures(
    bulk_upload_job_id: UUID,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    service: BulkUploadMonitoringService = Depends(get_bulk_upload_monitoring_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    """Read-only monitoring endpoint — paginated list of this job's failed files, with resolved failure detail per file."""
    return APIResponse.ok(
        data=service.get_job_failures(bulk_upload_job_id, page=page, size=size),
        message="Bulk upload job failures retrieved successfully.",
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
