from uuid import UUID
from fastapi import APIRouter, Depends, Query, Security, status

from app.dependencies.jd import get_jd_service
from app.enums.constants import UserRole
from app.middleware.rbac import TokenUser, require_roles
from app.schemas.jd.request import CreateJDRequest, UpdateJDRequest,  JDSearchRequest
from app.schemas.jd.response import CreateJDResponse, GetJDResponse, UpdateJDResponse, PaginatedJDResponse
from app.services.jd.jd_service import JDService
from app.schemas.response import APIResponse

router = APIRouter(
    prefix="/job-descriptions",
    tags=["Job Descriptions"],
)


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


@router.put(
    "/{jd_id}",
    response_model=APIResponse,
    status_code=status.HTTP_200_OK,
    )
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
    # dependencies=[Security(require_roles(UserRole.RECRUITER))]
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