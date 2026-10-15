"""Certification -> skill mappings (Phase 3 Step 5): HR_ADMIN only."""
from uuid import UUID

from fastapi import APIRouter, Depends, Security, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies.campaign import get_audit_service
from app.middleware.rbac import TokenUser, require_roles, resolve_actor_role
from app.models.identity import UserRole
from app.repositories.certification_repository import CertificationRepository
from app.repositories.skill_detection_repository import SkillDetectionRepository
from app.repositories.skill_repository import SkillRepository
from app.schemas.certification import (
    CertificationCreate,
    CertificationItem,
    CertificationSkillCreate,
    CertificationSkillItem,
    SenseOptionItem,
    SkillMeaningsItem,
)
from app.schemas.response import APIResponse
from app.services.audit_service import AuditService
from app.services.skills.certification_mapping_service import CertificationMappingService

router = APIRouter(prefix="/certifications", tags=["Certifications"])


def get_certification_mapping_service(
    db: Session = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
) -> CertificationMappingService:
    return CertificationMappingService(CertificationRepository(db), SkillRepository(db),
                                       SkillDetectionRepository(db).load_senses, audit_service)


def _item(certification, skill_names: dict) -> CertificationItem:
    return CertificationItem(
        id=certification.id, name=certification.name, name_variants=list(certification.name_variants or []),
        note=certification.note, created_at=certification.created_at,
        skills=[CertificationSkillItem(canonical_skill_id=s.canonical_skill_id,
                                       skill=skill_names.get(s.canonical_skill_id), sense_key=s.sense_key)
                for s in certification.skills],
    )


def _names(service: CertificationMappingService, certifications) -> dict:
    ids = {s.canonical_skill_id for c in certifications for s in c.skills}
    skills = (service.skill_repository.get_skill_by_id(i) for i in ids)
    return {s.id: s.canonical_name for s in skills if s is not None}


@router.get("", response_model=APIResponse[list[CertificationItem]], status_code=status.HTTP_200_OK,
            summary="Certifications and the skills they show (HR_ADMIN)")
def list_certifications(
    service: CertificationMappingService = Depends(get_certification_mapping_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    certifications = service.list_all()
    names = _names(service, certifications)
    return APIResponse.ok(data=[_item(c, names) for c in certifications], message="Certifications retrieved.")


@router.get("/meanings", response_model=APIResponse[list[SkillMeaningsItem]], status_code=status.HTTP_200_OK,
            summary="Ambiguous skills and their meanings, for choosing a mapping's meaning (HR_ADMIN)")
def list_skill_meanings(
    service: CertificationMappingService = Depends(get_certification_mapping_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    by_skill: dict = {}
    for sense in service.senses_loader():
        by_skill.setdefault(sense.skill_id, []).append(SenseOptionItem(sense_key=sense.sense_key, label=sense.label))
    items = []
    for skill_id, senses in by_skill.items():
        if len(senses) < 2:
            continue
        skill = service.skill_repository.get_skill_by_id(skill_id)
        items.append(SkillMeaningsItem(canonical_skill_id=skill_id, skill=getattr(skill, "canonical_name", None),
                                       senses=senses))
    return APIResponse.ok(data=items, message="Skill meanings retrieved.")


@router.post("", response_model=APIResponse[CertificationItem], status_code=status.HTTP_200_OK,
             summary="Add a certification with its name variants (HR_ADMIN)")
def add_certification(
    request: CertificationCreate,
    service: CertificationMappingService = Depends(get_certification_mapping_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    certification = service.add_certification(name=request.name, variants=request.name_variants, note=request.note,
                                              actor_id=user.user_id, actor_role=resolve_actor_role(user))
    return APIResponse.ok(data=_item(certification, {}), message="Certification added.")


@router.delete("/{certification_id}", response_model=APIResponse[None], status_code=status.HTTP_200_OK,
               summary="Remove a certification and its skill mappings (HR_ADMIN)")
def remove_certification(
    certification_id: UUID,
    service: CertificationMappingService = Depends(get_certification_mapping_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    service.remove_certification(certification_id, actor_id=user.user_id, actor_role=resolve_actor_role(user))
    return APIResponse.ok(message="Certification removed.")


@router.post("/{certification_id}/skills", response_model=APIResponse[None], status_code=status.HTTP_200_OK,
             summary="Map a skill (optionally a meaning) from a certification (HR_ADMIN)")
def add_certification_skill(
    certification_id: UUID,
    request: CertificationSkillCreate,
    service: CertificationMappingService = Depends(get_certification_mapping_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    service.add_skill(certification_id, canonical_skill_id=request.canonical_skill_id, sense_key=request.sense_key,
                      actor_id=user.user_id, actor_role=resolve_actor_role(user))
    return APIResponse.ok(message="Skill mapped.")


@router.delete("/{certification_id}/skills/{canonical_skill_id}", response_model=APIResponse[None],
               status_code=status.HTTP_200_OK, summary="Remove a skill from a certification (HR_ADMIN)")
def remove_certification_skill(
    certification_id: UUID,
    canonical_skill_id: UUID,
    service: CertificationMappingService = Depends(get_certification_mapping_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    service.remove_skill(certification_id, canonical_skill_id, actor_id=user.user_id,
                         actor_role=resolve_actor_role(user))
    return APIResponse.ok(message="Skill removed from the certification.")
