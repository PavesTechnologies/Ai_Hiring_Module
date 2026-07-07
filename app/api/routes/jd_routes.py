from typing import Optional
from uuid import UUID
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

from app.dependencies.jd import get_jd_service
from app.enums.constants import UserRole
from app.middleware.rbac import TokenUser, require_roles
from app.schemas.jd.request import CreateJDRequest, EducationCriteria, UpdateJDRequest, JDSearchRequest
from app.schemas.jd.response import CreateJDResponse, GetJDResponse, UpdateJDResponse, PaginatedJDResponse
from app.services.jd.jd_service import JDService
from app.schemas.response import APIResponse
from fastapi.responses import StreamingResponse

router = APIRouter(
    prefix="/job-descriptions",
    tags=["Job Descriptions"],
)

# Placeholder tenant until org resolution is wired into the JWT — same
# stand-in constant used by campaign_routes.SYSTEM_ORG.
SYSTEM_ORG = UUID("11111111-1111-1111-1111-111111111111")


@router.post(
    "",
    response_model=APIResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_job_description(
    request: CreateJDRequest,
    service: JDService = Depends(get_jd_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    response = service.create_jd(
        request=request,
        created_by=user.user_id
    )
    return APIResponse.ok(data=response, message="Job Description created successfully.")


@router.post(
    "/from-file",
    response_model=APIResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_job_description_from_file(
    title: str = Form(..., min_length=1, max_length=255),
    jurisdiction: str = Form(...),
    min_experience_years: Optional[float] = Form(default=None),
    education_degree: Optional[str] = Form(default=None),
    education_field: Optional[str] = Form(default=None),
    file: UploadFile = File(...),
    service: JDService = Depends(get_jd_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    """
    PDF/DOCX upload counterpart to create_job_description(). Validates and
    stores the file, extracts its text, then delegates to the same
    JDService.create_jd() used by the JSON endpoint for hashing, duplicate
    detection, saving, and audit — no business logic is duplicated here.
    """
    raw_text, file_path = service.process_uploaded_file(file=file, org_id=SYSTEM_ORG)

    jd_request = CreateJDRequest(
        title=title,
        raw_text=raw_text,
        jurisdiction=jurisdiction,
        min_experience_years=min_experience_years,
        education_criteria=(
            EducationCriteria(degree=education_degree, field=education_field)
            if education_degree or education_field
            else None
        ),
    )

    response = service.create_jd(
        request=jd_request,
        created_by=user.user_id,
        file_path=file_path,
    )
    return APIResponse.ok(data=response, message="Job Description created successfully from uploaded document.")

@router.get("/export")
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

@router.get("/{jd_id}/export")
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


@router.get("/all-active-jds", response_model=APIResponse[list[GetJDResponse]],)
def get_all_active_jds(
    service: JDService = Depends(get_jd_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
):
    return APIResponse.ok(data=service.get_all_jds(is_active_version=True), message="Active Job Descriptions retrieved successfully.")


@router.get("/{jd_id}", response_model=APIResponse,)
def get_job_description_by_id(
    jd_id: str,
    service: JDService = Depends(get_jd_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
):
    response = service.get_by_id(jd_id=jd_id)
    return APIResponse.ok(data=response, message="Job Description retrieved successfully.")


@router.get("/{jd_id}/download")
def download_job_description_file(
    jd_id: UUID,
    service: JDService = Depends(get_jd_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
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


@router.put("/{jd_id}",response_model=APIResponse, status_code=status.HTTP_200_OK,)
def update_job_description(
    jd_id: UUID,
    request: UpdateJDRequest,
    service: JDService = Depends(get_jd_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    response = service.update_jd(
        jd_id=jd_id,
        request=request,
        updated_by=user.user_id
    )
    return APIResponse.ok(data=response, message="Job Description updated successfully.")


@router.put(
    "/{jd_id}/from-file",
    response_model=APIResponse,
    status_code=status.HTTP_200_OK,
)
def update_job_description_from_file(
    jd_id: UUID,
    title: str = Form(..., min_length=1, max_length=255),
    jurisdiction: str = Form(...),
    min_experience_years: Optional[float] = Form(default=None),
    education_degree: Optional[str] = Form(default=None),
    education_field: Optional[str] = Form(default=None),
    file: UploadFile = File(...),
    service: JDService = Depends(get_jd_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    """
    PDF/DOCX upload counterpart to update_job_description(). Validates and
    stores the new file, extracts its text, then delegates to the same
    JDService.update_jd() used by the JSON endpoint — no business logic is
    duplicated here. The previous document (if any) is deleted from Storage
    only after the new version has committed successfully.
    """
    raw_text, file_path = service.process_uploaded_file(file=file, org_id=SYSTEM_ORG)

    jd_request = UpdateJDRequest(
        title=title,
        raw_text=raw_text,
        jurisdiction=jurisdiction,
        min_experience_years=min_experience_years,
        education_criteria=(
            EducationCriteria(degree=education_degree, field=education_field)
            if education_degree or education_field
            else None
        ),
    )

    response = service.update_jd(
        jd_id=jd_id,
        request=jd_request,
        updated_by=user.user_id,
        file_path=file_path,
    )
    return APIResponse.ok(data=response, message="Job Description updated successfully from uploaded document.")


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
)
def search_job_descriptions(
    service: JDService = Depends(get_jd_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),

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

