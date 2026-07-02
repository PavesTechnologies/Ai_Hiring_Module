from uuid import UUID
from fastapi import APIRouter, Depends, Query, Security, status

from app.dependencies.jd import get_jd_service
from app.enums.constants import UserRole
from app.middleware.rbac import TokenUser, require_roles
from app.schemas.jd.request import CreateJDRequest, UpdateJDRequest,  JDSearchRequest
from app.schemas.jd.response import CreateJDResponse, GetJDResponse, UpdateJDResponse, PaginatedJDResponse
from app.services.jd.jd_service import JDService

router = APIRouter(
    prefix="/job-descriptions",
    tags=["Job Descriptions"],
)

SYSTEM_USER = UUID("22222222-2222-2222-2222-222222222222")


@router.post(
    "",
    response_model=CreateJDResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_job_description(
    request: CreateJDRequest,
    service: JDService = Depends(get_jd_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    return service.create_jd(
        request=request,
        created_by=SYSTEM_USER
    )



@router.get("/all-active-jds", response_model=list[GetJDResponse],)
def get_all_active_jds(
    service: JDService = Depends(get_jd_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
):
    return service.get_all_jds(is_active_version=True)

@router.get("/{jd_id}", response_model=GetJDResponse,)
def get_job_description_by_id(
    jd_id: str,
    service: JDService = Depends(get_jd_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
):
    return service.get_by_id(jd_id=jd_id)


@router.put(
    "/{jd_id}",
    response_model=UpdateJDResponse,
    status_code=status.HTTP_200_OK,
    )
def update_job_description(
    jd_id: UUID,
    request: UpdateJDRequest,
    service: JDService = Depends(get_jd_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    return service.update_jd(
        jd_id=jd_id,
        request=request,
        updated_by=SYSTEM_USER
    )


@router.delete(
    "/{jd_id}",
    response_model=UpdateJDResponse,
    status_code=status.HTTP_200_OK
)
def delete_job_description(
    jd_id: UUID,
    service: JDService = Depends(get_jd_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    return service.deactivate_jd(
        jd_id=jd_id,
        updated_by=SYSTEM_USER
    )


@router.get(
    "",
    response_model=PaginatedJDResponse,
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

    return service.search_job_descriptions(request)