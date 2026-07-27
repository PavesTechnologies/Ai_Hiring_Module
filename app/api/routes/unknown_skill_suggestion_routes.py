from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Security, status

from app.dependencies.unknown_skill_suggestion import get_unknown_skill_suggestion_service
from app.middleware.rbac import TokenUser, require_roles
from app.models.identity import UserRole
from app.schemas.response import APIResponse
from app.schemas.unknown_skill.skill_suggestion_response import SkillSuggestionResponse
from app.services.skills.unknown_skill_suggestion_service import UnknownSkillSuggestionService

router = APIRouter(prefix="/unknown-skills", tags=["Unknown Skill Suggestions"])


@router.get(
    "/{unknown_skill_id}/suggestions/rapidfuzz-canonical",
    response_model=APIResponse[list[SkillSuggestionResponse]],
    status_code=status.HTTP_200_OK,
)
def get_rapidfuzz_canonical_suggestions(
    unknown_skill_id: UUID,
    service: UnknownSkillSuggestionService = Depends(get_unknown_skill_suggestion_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
    limit: Optional[int] = Query(default=None, ge=1, le=100),
    threshold: Optional[float] = Query(default=None, ge=0),
):
    data = service.get_rapidfuzz_canonical_suggestions(unknown_skill_id, limit=limit, threshold=threshold)
    return APIResponse.ok(data=data, message="Suggestions fetched successfully.")


@router.get(
    "/{unknown_skill_id}/suggestions/semantic-canonical",
    response_model=APIResponse[list[SkillSuggestionResponse]],
    status_code=status.HTTP_200_OK,
)
def get_semantic_canonical_suggestions(
    unknown_skill_id: UUID,
    service: UnknownSkillSuggestionService = Depends(get_unknown_skill_suggestion_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
    limit: Optional[int] = Query(default=None, ge=1, le=100),
    threshold: Optional[float] = Query(default=None, ge=0),
):
    data = service.get_semantic_canonical_suggestions(unknown_skill_id, limit=limit, threshold=threshold)
    return APIResponse.ok(data=data, message="Suggestions fetched successfully.")


@router.get(
    "/{unknown_skill_id}/suggestions/rapidfuzz-alias",
    response_model=APIResponse[list[SkillSuggestionResponse]],
    status_code=status.HTTP_200_OK,
)
def get_rapidfuzz_alias_suggestions(
    unknown_skill_id: UUID,
    service: UnknownSkillSuggestionService = Depends(get_unknown_skill_suggestion_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
    limit: Optional[int] = Query(default=None, ge=1, le=100),
    threshold: Optional[float] = Query(default=None, ge=0),
):
    data = service.get_rapidfuzz_alias_suggestions(unknown_skill_id, limit=limit, threshold=threshold)
    return APIResponse.ok(data=data, message="Suggestions fetched successfully.")


@router.get(
    "/{unknown_skill_id}/suggestions/semantic-alias",
    response_model=APIResponse[list[SkillSuggestionResponse]],
    status_code=status.HTTP_200_OK,
)
def get_semantic_alias_suggestions(
    unknown_skill_id: UUID,
    service: UnknownSkillSuggestionService = Depends(get_unknown_skill_suggestion_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
    limit: Optional[int] = Query(default=None, ge=1, le=100),
    threshold: Optional[float] = Query(default=None, ge=0),
):
    data = service.get_semantic_alias_suggestions(unknown_skill_id, limit=limit, threshold=threshold)
    return APIResponse.ok(data=data, message="Suggestions fetched successfully.")
