from typing import Optional
from uuid import UUID, uuid4
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Query,
    Response,
    Security,
    UploadFile,
    status,
)

from app.dependencies.jd import (
    get_celery_task_log_service,
    get_hash_service,
    get_jd_processing_status_service,
    get_jd_repository,
    get_jd_service,
    get_stage_tracker,
)
from app.enums.constants import UserRole
from app.exceptions.duplicate_jd_exception import DuplicateJDException
from app.middleware.rbac import TokenUser, require_roles
from app.models.async_tasks import DocumentType, ProcessingStage
from app.repositories.jd_repository import JDRepository
from app.schemas.jd.DuplicateJDInfo import DuplicateJDInfo, ExistingJDInfo
from app.schemas.jd.request import CreateJDRequest, EducationCriteria, UpdateJDRequest, JDSearchRequest
from app.schemas.jd.response import (
    GetJDResponse,
    JDProcessingAcceptedResponse,
    JDProcessingStatusResponse,
    JDUploadSummary,
    PaginatedJDResponse,
    UpdateJDResponse,
)
from app.services.celery_task_log_service import CeleryTaskLogService
from app.services.document_processing.stage_execution_service import StageExecutionService
from app.services.jd.hash_service import HashService
from app.services.jd.jd_processing_status_service import JDProcessingStatusService
from app.services.jd.jd_service import JDReprocessRequired, JDService
from app.tasks.jd_processing_tasks import process_jd_document
from app.schemas.response import APIResponse
from fastapi import Query
from app.middleware.rbac import TokenUser, require_roles
from app.models.identity import UserRole
from fastapi.responses import StreamingResponse

router = APIRouter(
    prefix="/job-descriptions",
    tags=["Job Descriptions"],
)

# Placeholder tenant until org resolution is wired into the JWT — same
# stand-in constant used by campaign_routes.SYSTEM_ORG.
SYSTEM_ORG = UUID("11111111-1111-1111-1111-111111111111")


def _queue_reprocess(
    result: JDReprocessRequired,
    response: Response,
    stage_tracker: StageExecutionService,
    task_log_service: CeleryTaskLogService,
) -> APIResponse:
    task_id = uuid4()
    stage_tracker.run_stage(str(task_id), DocumentType.JD, ProcessingStage.VALIDATION, lambda: None)
    task_log_service.create_log(
        task_id=str(task_id),
        task_type="JD_DOCUMENT_PROCESSING",
        created_by=result.updated_by,
        title=result.title,
    )

    process_jd_document.apply_async(
        kwargs={
            "task_id": str(task_id),
            "raw_text": result.raw_text,
            "file_path": result.file_path,
            "original_filename": result.original_filename,
            "title": result.title,
            "jurisdiction": result.jurisdiction,
            "min_experience_years": result.min_experience_years,
            "max_experience_years": result.max_experience_years,
            "notice_period": result.notice_period,
            "education_criteria": result.education_criteria,
            "created_by": result.updated_by,
            "existing_jd_id": str(result.existing_jd_id),
            "version_number": result.version_number,
            "parent_jd_id": str(result.parent_jd_id),
            "lineage_root_id": str(result.lineage_root_id),
            "old_file_path": result.old_file_path,
        },
        task_id=str(task_id),
    )

    response.status_code = status.HTTP_202_ACCEPTED
    return APIResponse.ok(
        data=JDProcessingAcceptedResponse(task_id=task_id, status="QUEUED"),
        message="Job Description update submitted for reprocessing.",
    )


def _raise_if_duplicate(jd_repository: JDRepository, content_hash: str) -> None:
    existing_jd = jd_repository.get_by_content_hash(content_hash)
    if existing_jd:
        raise DuplicateJDException(
            DuplicateJDInfo(
                message="Duplicate job description found.",
                existing_jd=ExistingJDInfo(
                    id=existing_jd.id,
                    title=existing_jd.title,
                    version_number=existing_jd.version_number,
                    created_at=existing_jd.created_at,
                ),
                actions=["View Existing", "Create New Version"],
            )
        )


@router.post(
    "",
    response_model=APIResponse[JDProcessingAcceptedResponse],
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Security(require_roles(UserRole.HR_ADMIN))],
)
def create_job_description(
    request: CreateJDRequest,
    jd_repository: JDRepository = Depends(get_jd_repository),
    hash_service: HashService = Depends(get_hash_service),
    stage_tracker: StageExecutionService = Depends(get_stage_tracker),
    task_log_service: CeleryTaskLogService = Depends(get_celery_task_log_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    """
    Validation runs synchronously (raw_text is already in hand, so the
    duplicate pre-check is cheap and gives an immediate 409). The rest of
    the pipeline — Text Cleaning through Persistence — runs in the
    background; the JobDescription row itself is only created there.
    """
    content_hash = hash_service.generate_hash(request.raw_text)
    _raise_if_duplicate(jd_repository, content_hash)

    task_id = uuid4()
    stage_tracker.run_stage(str(task_id), DocumentType.JD, ProcessingStage.VALIDATION, lambda: None)
    task_log_service.create_log(
        task_id=str(task_id),
        task_type="JD_DOCUMENT_PROCESSING",
        created_by=user.user_id,
        title=request.title,
    )

    process_jd_document.apply_async(
        kwargs={
            "task_id": str(task_id),
            "raw_text": request.raw_text,
            "file_path": None,
            "original_filename": None,
            "title": request.title,
            "jurisdiction": request.jurisdiction,
            "min_experience_years": request.min_experience_years,
            "max_experience_years": request.max_experience_years,
            "notice_period": request.notice_period,
            "education_criteria": request.education_criteria.model_dump(),
            "created_by": user.user_id,
        },
        task_id=str(task_id),
    )

    return APIResponse.ok(
        data=JDProcessingAcceptedResponse(task_id=task_id, status="QUEUED"),
        message="Job Description submitted for processing.",
    )


@router.post(
    "/from-file",
    response_model=APIResponse[JDProcessingAcceptedResponse],
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Security(require_roles(UserRole.HR_ADMIN))]
)
def create_job_description_from_file(
    title: str = Form(..., min_length=1, max_length=255),
    jurisdiction: str = Form(...),
    min_experience_years: float = Form(...),
    max_experience_years: float = Form(...),
    notice_period: int = Form(...),
    education_degree: Optional[str] = Form(default=None),
    education_field: Optional[str] = Form(default=None),
    file: UploadFile = File(...),
    service: JDService = Depends(get_jd_service),
    stage_tracker: StageExecutionService = Depends(get_stage_tracker),
    task_log_service: CeleryTaskLogService = Depends(get_celery_task_log_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    """
    PDF/DOCX upload counterpart to create_job_description(). Validation and
    Storage run synchronously here (the file must be safely stored before
    the response returns); Text Extraction through Persistence — including
    the file-path duplicate check — run in the background.
    """
    task_id = uuid4()
    stage_tracker.run_stage(
        str(task_id), DocumentType.JD, ProcessingStage.VALIDATION,
        lambda: service.validate_upload_type(file),
    )

    file_path, original_filename = stage_tracker.run_stage(
        str(task_id), DocumentType.JD, ProcessingStage.STORAGE,
        lambda: service.validate_and_store_file(file=file, org_id=SYSTEM_ORG),
    )
    task_log_service.create_log(
        task_id=str(task_id),
        task_type="JD_DOCUMENT_PROCESSING",
        created_by=user.user_id,
        title=title,
    )

    education_criteria = {"degree": education_degree, "field": education_field}

    process_jd_document.apply_async(
        kwargs={
            "task_id": str(task_id),
            "raw_text": None,
            "file_path": file_path,
            "original_filename": original_filename,
            "title": title,
            "jurisdiction": jurisdiction,
            "min_experience_years": min_experience_years,
            "max_experience_years": max_experience_years,
            "notice_period": notice_period,
            "education_criteria": education_criteria,
            "created_by": user.user_id,
        },
        task_id=str(task_id),
    )

    return APIResponse.ok(
        data=JDProcessingAcceptedResponse(task_id=task_id, status="QUEUED"),
        message="Job Description document submitted for processing.",
    )


@router.get(
    "/processing-status/{task_id}",
    response_model=APIResponse[JDProcessingStatusResponse],
    status_code=status.HTTP_200_OK,
    dependencies=[Security(require_roles(UserRole.HR_ADMIN))],
)
def get_jd_processing_status(
    task_id: UUID,
    service: JDProcessingStatusService = Depends(get_jd_processing_status_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
):
    return APIResponse.ok(
        data=service.get_status(task_id),
        message="Processing status retrieved successfully.",
    )


@router.get(
    "/my-uploads",
    response_model=APIResponse[list[JDUploadSummary]],
    status_code=status.HTTP_200_OK,
    dependencies=[Security(require_roles(UserRole.HR_ADMIN))],
)
def get_my_jd_uploads(
    service: JDProcessingStatusService = Depends(get_jd_processing_status_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
):
    """
    Every JD create/reprocess task the current user has submitted, newest
    first — lets a user check back later whether an upload succeeded,
    is still processing, or failed, without needing to hold onto its
    task_id (which /processing-status/{task_id} otherwise requires).
    """
    return APIResponse.ok(
        data=service.get_recent_uploads(user.user_id),
        message="Recent uploads retrieved successfully.",
    )


@router.get("/export", status_code=status.HTTP_200_OK, dependencies=[Security(require_roles(UserRole.HR_ADMIN))])
def export_job_descriptions(
    service: JDService = Depends(get_jd_service),
    user: TokenUser = Security(
        require_roles(
            UserRole.HR_ADMIN,
        )
    ),
    search: str | None = Query(default=None),
    jurisdiction: str | None = Query(default=None),
    active: bool | None = Query(default=True),
    source_format: str | None = Query(default=None),
    sort_by: str = Query(default="created_at"),
    order: str = Query(default="desc"),
):

    request = JDSearchRequest(
        search=search,
        jurisdiction=jurisdiction,
        active=active,
        source_format=source_format,
        page=1,
        size=1,
        sort_by=sort_by,
        order=order,
    )

    return service.export_jd_list(
        request=request,
        exported_by=user.user_id,
        actor_role=user.roles[0] if user.roles else None,
    )

@router.get("/{jd_id}/export", status_code=status.HTTP_200_OK, dependencies=[Security(require_roles(UserRole.HR_ADMIN))])
def export_single_job_description(
    jd_id: UUID,
    service: JDService = Depends(get_jd_service),
    user: TokenUser = Security(
        require_roles(
            UserRole.HR_ADMIN,
        )
    ),
):
    return service.export_single_jd(
        jd_id=jd_id,
        exported_by=user.user_id,
        actor_role=user.roles[0] if user.roles else None,
    )


@router.get("/all-active-jds", response_model=APIResponse[list[GetJDResponse]],dependencies=[Security(require_roles(UserRole.HR_ADMIN))])
def get_all_active_jds(
    service: JDService = Depends(get_jd_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
):
    return APIResponse.ok(data=service.get_all_jds(is_active_version=True), message="Active Job Descriptions retrieved successfully.")


@router.get("/{jd_id}", response_model=APIResponse,dependencies=[Security(require_roles(UserRole.HR_ADMIN))])
def get_job_description_by_id(
    jd_id: str,
    service: JDService = Depends(get_jd_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
):
    response = service.get_by_id(jd_id=jd_id)
    return APIResponse.ok(data=response, message="Job Description retrieved successfully.")


@router.get("/{jd_id}/download", status_code=status.HTTP_200_OK, dependencies=[Security(require_roles(UserRole.HR_ADMIN))])
def download_job_description_file(
    jd_id: UUID,
    service: JDService = Depends(get_jd_service)
):
    """
    Downloads the JD's document: the original PDF/DOCX if one was uploaded,
    or the raw_text rendered into a DOCX on the fly if the JD is TEXT-sourced.
    Returns the raw file bytes directly (not wrapped in APIResponse), since
    this is a binary file download rather than a JSON API response.
    """
    file_bytes, filename, content_type = service.download_jd_file(jd_id=jd_id)
    return Response(
        content=file_bytes,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{jd_id}/view", status_code=status.HTTP_200_OK, dependencies=[Security(require_roles(UserRole.HR_ADMIN))])
def view_job_description_file(
    jd_id: UUID,
    service: JDService = Depends(get_jd_service)
):
    """
    Same underlying file as /download (original PDF/DOCX, or raw_text
    rendered into a DOCX for TEXT-sourced JDs), but with an inline
    Content-Disposition so the browser renders it directly - e.g. to show
    the existing document in the update form - instead of forcing a
    Save-As download.
    """
    file_bytes, filename, content_type = service.download_jd_file(jd_id=jd_id)
    return Response(
        content=file_bytes,
        media_type=content_type,
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.put("/{jd_id}",response_model=APIResponse, status_code=status.HTTP_200_OK,)
def update_job_description(
    jd_id: UUID,
    request: UpdateJDRequest,
    response: Response,
    service: JDService = Depends(get_jd_service),
    stage_tracker: StageExecutionService = Depends(get_stage_tracker),
    task_log_service: CeleryTaskLogService = Depends(get_celery_task_log_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """
    A metadata-only update (raw_text unchanged) returns 200 with the
    updated JD, same as before. If raw_text actually changed, this queues
    the same async pipeline JD creation uses and returns 202 + task_id —
    Extraction/Normalization/Matching/Embedding are too slow to run inline.
    """
    result = service.update_jd(jd_id=jd_id, request=request, updated_by=user.user_id)

    if isinstance(result, JDReprocessRequired):
        return _queue_reprocess(result, response, stage_tracker, task_log_service)

    return APIResponse.ok(data=result, message="Job Description updated successfully.")


@router.put(
    "/{jd_id}/from-file",
    response_model=APIResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def update_job_description_from_file(
    jd_id: UUID,
    response: Response,
    title: str = Form(..., min_length=1, max_length=255),
    jurisdiction: str = Form(...),
    min_experience_years: float = Form(...),
    max_experience_years: float = Form(...),
    notice_period: int = Form(...),
    education_degree: Optional[str] = Form(default=None),
    education_field: Optional[str] = Form(default=None),
    file: UploadFile = File(...),
    service: JDService = Depends(get_jd_service),
    stage_tracker: StageExecutionService = Depends(get_stage_tracker),
    task_log_service: CeleryTaskLogService = Depends(get_celery_task_log_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """
    PDF/DOCX upload counterpart to update_job_description(). Validates and
    stores the new file synchronously (must be safely stored before the
    response returns), then delegates to JDService.update_jd() — no
    business logic duplicated here. A new file always means reprocessing
    (this is one of the two triggers), so text extraction happens in the
    pipeline's own stage, same as JD creation, rather than in the route.
    """
    file_path, original_filename = service.validate_and_store_file(file=file, org_id=SYSTEM_ORG)

    jd_request = UpdateJDRequest(
        title=title,
        raw_text=None,
        jurisdiction=jurisdiction,
        min_experience_years=min_experience_years,
        max_experience_years=max_experience_years,
        notice_period=notice_period,
        education_criteria=EducationCriteria(degree=education_degree, field=education_field),
    )

    result = service.update_jd(
        jd_id=jd_id,
        request=jd_request,
        updated_by=user.user_id,
        file_path=file_path,
        original_filename=original_filename,
    )
    # file_path is always set here, so update_jd() always returns
    # JDReprocessRequired for this route — never the synchronous shape.
    return _queue_reprocess(result, response, stage_tracker, task_log_service)


@router.delete(
    "/{jd_id}",
    response_model=APIResponse,
    status_code=status.HTTP_200_OK
)
def delete_job_description(
    jd_id: UUID,
    service: JDService = Depends(get_jd_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    response = service.deactivate_jd(
        jd_id=jd_id,
        updated_by=user.user_id
    )
    return APIResponse.ok(data=response, message="Job Description deactivated successfully.")


@router.get(
    "",
    response_model=APIResponse[PaginatedJDResponse],
    status_code=status.HTTP_200_OK,
    dependencies=[Security(require_roles(UserRole.HR_ADMIN))]
)
def search_job_descriptions(
    service: JDService = Depends(get_jd_service),

    search: str | None = Query(default=None),
    jurisdiction: str | None = Query(default=None),
    active: bool | None = Query(default=True),
    source_format: str | None = Query(default=None),

    page: int = Query(default=1, ge=1),
    size: int = Query(default=10, ge=1, le=100),

    sort_by: str = Query(default="created_at"),
    order: str = Query(default="desc"),
):

    request = JDSearchRequest(
        search=search,
        jurisdiction=jurisdiction,
        active=active,
        source_format=source_format,
        page=page,
        size=size,
        sort_by=sort_by,
        order=order,
    )

    response = service.search_job_descriptions(request)
    return APIResponse.ok(data=response, message="Job Descriptions searched successfully.")

