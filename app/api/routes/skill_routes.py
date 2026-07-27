from uuid import UUID

from fastapi import APIRouter, Depends, Query, Security

from app.dependencies.skills import get_skill_curation_service
from app.middleware.rbac import TokenUser, require_roles
from app.models.identity import UserRole
from app.schemas.response import APIResponse
from app.schemas.skills.curation import (
    BulkUnknownSkillActionResponse,
    BulkUnknownSkillIdsRequest,
    BulkUnknownSkillResultItem,
    CreateCanonicalSkillFromUnknownRequest,
    CreateCanonicalSkillFromUnknownResponse,
    JDSkillItem,
    JDSkillRemapResponse,
    JDUnknownSkillItem,
    MapUnknownSkillRequest,
    PromoteUnknownSkillRequest,
    PromotedSkillResponse,
    RemapJDSkillRequest,
    UnknownSkillActionResponse,
    UnknownSkillCandidateItem,
    UnknownSkillDeleteResponse,
    UnknownSkillItem,
    UnknownSkillJDItem,
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
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
    search: str | None = Query(default=None),
):
    """HR review queue — pending/under-review UnknownSkill entries, highest-frequency first."""
    unknown_skills = service.list_pending_unknown_skills(
        page=page, page_size=page_size, search=search,
    )
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
    "/unknown/{unknown_skill_id}/create-canonical",
    response_model=APIResponse[CreateCanonicalSkillFromUnknownResponse],
)
def create_canonical_skill_from_unknown(
    unknown_skill_id: UUID,
    request: CreateCanonicalSkillFromUnknownRequest,
    service: SkillCurationService = Depends(get_skill_curation_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """
    HR promotes an unknown skill straight into a fully-specified canonical
    skill (aliases/category/parent/confidence/source/is_active all
    HR-supplied, unlike /promote's bare-bones category-only version). Every
    JD/candidate occurrence still linked to it is migrated onto the new
    skill as verified, and the UnknownSkill row is then hard-deleted.
    """
    result = service.create_canonical_skill_from_unknown(
        unknown_skill_id=unknown_skill_id,
        actor_id=user.user_id,
        canonical_name=request.canonical_name,
        aliases=request.aliases,
        category=request.category,
        parent_skill_id=request.parent_skill_id,
        confidence=request.confidence,
        source=request.source,
        is_active=request.is_active,
    )
    new_skill = result["skill"]
    return APIResponse.ok(
        data=CreateCanonicalSkillFromUnknownResponse(
            id=new_skill.id,
            canonical_name=new_skill.canonical_name,
            aliases=new_skill.aliases or [],
            category=new_skill.category,
            parent_skill_id=new_skill.parent_skill_id,
            confidence=new_skill.confidence,
            source=new_skill.source,
            is_active=new_skill.is_active,
            jd_skills_migrated=result["jd_skills_migrated"],
            candidate_skills_migrated=result["candidate_skills_migrated"],
        ),
        message="Canonical skill created from unknown skill successfully.",
    )


@router.post(
    "/unknown/bulk-approve",
    response_model=APIResponse[BulkUnknownSkillActionResponse],
)
def bulk_approve_unknown_skills(
    request: BulkUnknownSkillIdsRequest,
    service: SkillCurationService = Depends(get_skill_curation_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """
    Bulk version of /unknown/{id}/create-canonical: each id in the list is
    promoted to its own new canonical skill (using its own raw_text - bulk
    mode takes no per-item aliases/category/etc overrides), every JD/
    candidate occurrence still linked to it is migrated onto that skill as
    verified, and the unknown skill row is then removed. Each id is
    processed independently, so one failure (already exists, not found)
    doesn't block the rest of the batch - check `results` for per-id outcome.
    """
    results = service.bulk_approve_unknown_skills(
        unknown_skill_ids=request.unknown_skill_ids,
        actor_id=user.user_id,
    )
    items = [BulkUnknownSkillResultItem(**result) for result in results]
    return APIResponse.ok(
        data=BulkUnknownSkillActionResponse(
            results=items,
            succeeded=sum(1 for item in items if item.success),
            failed=sum(1 for item in items if not item.success),
        ),
        message="Bulk approval completed.",
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


@router.delete(
    "/unknown/{unknown_skill_id}",
    response_model=APIResponse[UnknownSkillDeleteResponse],
)
def delete_unknown_skill(
    unknown_skill_id: UUID,
    service: SkillCurationService = Depends(get_skill_curation_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """
    Hard-deletes an UnknownSkill, its JDUnknownSkill links, and its
    CandidateSkill occurrences. Irreversible - allowed regardless of the
    unknown skill's current status. JDSkill is untouched: it has no
    reference to unknown_skills at all.
    """
    result = service.delete_unknown_skill(unknown_skill_id=unknown_skill_id, actor_id=user.user_id)
    return APIResponse.ok(
        data=UnknownSkillDeleteResponse(**result),
        message="Unknown skill deleted successfully.",
    )


@router.post(
    "/unknown/bulk-delete",
    response_model=APIResponse[BulkUnknownSkillActionResponse],
)
def bulk_delete_unknown_skills(
    request: BulkUnknownSkillIdsRequest,
    service: SkillCurationService = Depends(get_skill_curation_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """
    Bulk version of DELETE /unknown/{id}: hard-deletes every listed
    UnknownSkill along with its JDUnknownSkill links and CandidateSkill
    occurrences. Each id is processed independently, so one failure (not
    found) doesn't block the rest of the batch - check `results` for
    per-id outcome.
    """
    results = service.bulk_delete_unknown_skills(
        unknown_skill_ids=request.unknown_skill_ids,
        actor_id=user.user_id,
    )
    items = [BulkUnknownSkillResultItem(**result) for result in results]
    return APIResponse.ok(
        data=BulkUnknownSkillActionResponse(
            results=items,
            succeeded=sum(1 for item in items if item.success),
            failed=sum(1 for item in items if not item.success),
        ),
        message="Bulk delete completed.",
    )


@router.get(
    "/unknown/{unknown_skill_id}/jds",
    response_model=APIResponse[list[UnknownSkillJDItem]],
)
def list_jds_for_unknown_skill(
    unknown_skill_id: UUID,
    service: SkillCurationService = Depends(get_skill_curation_service),
    user: TokenUser = Security(
        require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)
    ),
):
    """Every JD (any version) this unknown skill occurs in — the reverse of /jd/{jd_id}/unknown-skills."""
    rows = service.list_jds_for_unknown_skill(unknown_skill_id)
    return APIResponse.ok(
        data=[
            UnknownSkillJDItem(
                id=link.id,
                jd_id=jd.id,
                job_id=jd.job_id,
                title=jd.title,
                version_number=jd.version_number,
                is_active_version=jd.is_active_version,
                mandatory=link.mandatory,
                status=link.status.value,
                created_at=link.created_at,
            )
            for link, jd in rows
        ],
        message="Job descriptions for unknown skill retrieved successfully.",
    )


@router.get(
    "/unknown/{unknown_skill_id}/candidates",
    response_model=APIResponse[list[UnknownSkillCandidateItem]],
)
def list_candidates_for_unknown_skill(
    unknown_skill_id: UUID,
    service: SkillCurationService = Depends(get_skill_curation_service),
    user: TokenUser = Security(
        require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)
    ),
):
    """
    Candidates whose (active) resume carries this exact unmatched raw skill
    text. Matched on raw text, not a shared id — resume-side unmatched
    skills are never deduped into unknown_skills the way JD-side ones are.
    """
    rows = service.list_candidates_for_unknown_skill(unknown_skill_id)
    return APIResponse.ok(
        data=[
            UnknownSkillCandidateItem(
                id=candidate_skill.id,
                candidate_id=candidate.id,
                resume_id=candidate_skill.resume_id,
                candidate_name=service.decrypt_candidate_name(candidate),
                raw_extracted_text=candidate_skill.raw_extracted_text,
                confidence=candidate_skill.confidence,
                match_tier=candidate_skill.match_tier,
                created_at=candidate_skill.created_at,
            )
            for candidate_skill, candidate, resume in rows
        ],
        message="Candidates for unknown skill retrieved successfully.",
    )


@router.get(
    "/jd/{jd_id}/skills",
    response_model=APIResponse[list[JDSkillItem]],
)
def list_jd_skills(
    jd_id: UUID,
    service: SkillCurationService = Depends(get_skill_curation_service),
    user: TokenUser = Security(
        require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)
    ),
):
    """Resolved (canonical) skills matched for a JD."""
    rows = service.list_jd_skills(jd_id)
    return APIResponse.ok(
        data=[
            JDSkillItem(
                id=jd_skill.id,
                jd_id=jd_skill.jd_id,
                canonical_skill_id=jd_skill.canonical_skill_id,
                canonical_name=skill.canonical_name,
                mandatory=jd_skill.mandatory,
                weight=jd_skill.weight,
                confidence=jd_skill.confidence,
                match_tier=jd_skill.match_tier,
                verification_status=jd_skill.verification_status.value,
                created_at=jd_skill.created_at,
            )
            for jd_skill, skill in rows
        ],
        message="JD skills retrieved successfully.",
    )


@router.get(
    "/jd/{jd_id}/unknown-skills",
    response_model=APIResponse[list[JDUnknownSkillItem]],
)
def list_jd_unknown_skills(
    jd_id: UUID,
    service: SkillCurationService = Depends(get_skill_curation_service),
    user: TokenUser = Security(
        require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)
    ),
):
    """Unknown-skill occurrences recorded for a JD, resolved or not."""
    rows = service.list_jd_unknown_skills(jd_id)
    return APIResponse.ok(
        data=[
            JDUnknownSkillItem(
                id=link.id,
                jd_id=link.jd_id,
                unknown_skill_id=link.unknown_skill_id,
                raw_text=unknown_skill.raw_text,
                mandatory=link.mandatory,
                status=link.status.value,
                created_at=link.created_at,
            )
            for link, unknown_skill in rows
        ],
        message="JD unknown skills retrieved successfully.",
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
