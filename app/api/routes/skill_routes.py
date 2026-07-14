from uuid import UUID

from fastapi import APIRouter, Depends, Security

from app.dependencies.skills import get_skill_curation_service
from app.middleware.rbac import TokenUser, require_roles
from app.models.identity import UserRole
from app.schemas.response import APIResponse
from app.schemas.skills.curation import (
    JDSkillRemapResponse,
    MapUnknownSkillRequest,
    PromoteUnknownSkillRequest,
    PromotedSkillResponse,
    RemapJDSkillRequest,
    UnknownSkillActionResponse,
    UnknownSkillItem,
)
from app.services.skills.skill_curation_service import SkillCurationService

router = APIRouter(
    prefix="/skills",
    tags=["Skill Ontology"],
)


@router.get(
    "/unknown",
    response_model=APIResponse[list[UnknownSkillItem]],
)
def list_pending_unknown_skills(
    service: SkillCurationService = Depends(get_skill_curation_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """HR review queue — pending/under-review UnknownSkill entries, highest-frequency first."""
    unknown_skills = service.list_pending_unknown_skills()
    return APIResponse.ok(
        data=[
            UnknownSkillItem(
                id=skill.id,
                raw_text=skill.raw_text,
                normalized_key=skill.normalized_key,
                frequency=skill.frequency,
                first_seen=skill.first_seen,
                last_seen=skill.last_seen,
                status=skill.status.value,
            )
            for skill in unknown_skills
        ],
        message="Pending unknown skills retrieved successfully.",
    )


@router.post(
    "/unknown/{unknown_skill_id}/map",
    response_model=APIResponse[UnknownSkillActionResponse],
)
def map_unknown_skill_to_existing(
    unknown_skill_id: UUID,
    request: MapUnknownSkillRequest,
    service: SkillCurationService = Depends(get_skill_curation_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """
    HR decides an unknown skill is a variant of an already-canonical one.
    Retroactively creates JDSkill rows for every JD still linked to it, and
    optionally records it as a new alias of the target skill.
    """
    unknown_skill = service.map_to_existing_skill(
        unknown_skill_id=unknown_skill_id,
        target_skill_id=request.target_skill_id,
        actor_id=user.user_id,
        save_as_alias=request.save_as_alias,
    )
    return APIResponse.ok(
        data=UnknownSkillActionResponse(
            id=unknown_skill.id, raw_text=unknown_skill.raw_text, status=unknown_skill.status.value,
        ),
        message="Unknown skill mapped to existing canonical skill.",
    )


@router.post(
    "/unknown/{unknown_skill_id}/promote",
    response_model=APIResponse[PromotedSkillResponse],
)
def promote_unknown_skill(
    unknown_skill_id: UUID,
    request: PromoteUnknownSkillRequest,
    service: SkillCurationService = Depends(get_skill_curation_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """
    HR decides an unknown skill is genuinely new. Creates it in the
    ontology and retroactively creates JDSkill rows for every JD still
    linked to it.
    """
    new_skill = service.promote_to_canonical(
        unknown_skill_id=unknown_skill_id,
        actor_id=user.user_id,
        category=request.category,
    )
    return APIResponse.ok(
        data=PromotedSkillResponse(id=new_skill.id, canonical_name=new_skill.canonical_name),
        message="Unknown skill promoted to a new canonical skill.",
    )


@router.post(
    "/unknown/{unknown_skill_id}/dismiss",
    response_model=APIResponse[UnknownSkillActionResponse],
)
def dismiss_unknown_skill(
    unknown_skill_id: UUID,
    service: SkillCurationService = Depends(get_skill_curation_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """HR decides an unknown skill isn't a real skill (junk extraction, etc)."""
    unknown_skill = service.dismiss(unknown_skill_id=unknown_skill_id, actor_id=user.user_id)
    return APIResponse.ok(
        data=UnknownSkillActionResponse(
            id=unknown_skill.id, raw_text=unknown_skill.raw_text, status=unknown_skill.status.value,
        ),
        message="Unknown skill dismissed.",
    )


@router.put(
    "/jd-skills/{jd_skill_id}/remap",
    response_model=APIResponse[JDSkillRemapResponse],
)
def remap_jd_skill(
    jd_skill_id: UUID,
    request: RemapJDSkillRequest,
    service: SkillCurationService = Depends(get_skill_curation_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """HR overrides an existing JDSkill's canonical mapping in place."""
    jd_skill = service.remap_jd_skill(
        jd_skill_id=jd_skill_id,
        new_canonical_skill_id=request.new_canonical_skill_id,
        actor_id=user.user_id,
    )
    return APIResponse.ok(
        data=JDSkillRemapResponse(
            id=jd_skill.id,
            jd_id=jd_skill.jd_id,
            canonical_skill_id=jd_skill.canonical_skill_id,
            match_tier=jd_skill.match_tier,
        ),
        message="JDSkill canonical mapping updated.",
    )
