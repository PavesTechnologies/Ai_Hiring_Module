from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Security, status

from app.dependencies.skills import get_skill_curation_service
from app.dependencies.unknown_skill import get_unknown_skill_service
from app.middleware.rbac import TokenUser, require_roles
from app.models.identity import UserRole
from app.schemas.response import APIResponse
from app.schemas.unknown_skill.skill_resolution_request import ResolveUnknownSkillRequest
from app.schemas.unknown_skill.unknown_skill_response import UnknownSkillPageResponse
from app.services.skills.skill_curation_service import SkillCurationService
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


@router.post(
    "/{unknown_skill_id}/resolve",
    response_model=APIResponse[None],
    status_code=status.HTTP_200_OK,
)
def resolve_unknown_skill(
    unknown_skill_id: UUID,
    request: ResolveUnknownSkillRequest,
    service: SkillCurationService = Depends(get_skill_curation_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """
    Maps a pending Unknown Skill onto an existing canonical skill, migrating
    every linked JD/candidate occurrence onto it. ADD_AS_ALIAS additionally
    records the unknown skill's raw text as a new alias first.
    """
    service.resolve_unknown_skill(
        unknown_skill_id=unknown_skill_id,
        canonical_skill_id=request.canonical_skill_id,
        resolution_type=request.type,
        actor_id=user.user_id,
    )
    return APIResponse.ok(data=None, message="Unknown Skill resolved successfully.")
