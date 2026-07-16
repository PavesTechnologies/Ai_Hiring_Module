from typing import Optional

from fastapi import APIRouter, Depends, Query, Security, status

from app.dependencies.unknown_skill import get_unknown_skill_service
from app.middleware.rbac import TokenUser, require_roles
from app.models.identity import UserRole
from app.schemas.response import APIResponse
from app.schemas.unknown_skill.unknown_skill_response import UnknownSkillPageResponse
from app.services.skills.unknown_skill_service import UnknownSkillService

router = APIRouter(prefix="/unknown-skills", tags=["Unknown Skills"])


@router.get(
    "",
    response_model=APIResponse[UnknownSkillPageResponse],
    status_code=status.HTTP_200_OK,
)
def list_unknown_skills(
    service: UnknownSkillService = Depends(get_unknown_skill_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    search: Optional[str] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
):
    result = service.get_unknown_skills(
        page=page,
        page_size=page_size,
        search=search,
        status=status_filter,
    )
    return APIResponse.ok(data=result, message="Unknown skills retrieved successfully.")
