"""Related education fields per role domain (Phase 3 Step 2): HR_ADMIN only."""
from uuid import UUID

from fastapi import APIRouter, Depends, Security, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies.campaign import get_audit_service
from app.enums.education import RoleDomain
from app.middleware.rbac import TokenUser, authorising_role, require_roles
from app.models.identity import UserRole
from app.repositories.education_field_relation_repository import EducationFieldRelationRepository
from app.schemas.education_field_relation import (
    EducationFieldRelationCreate,
    EducationFieldRelationItem,
    EducationFieldRelationList,
)
from app.schemas.response import APIResponse
from app.services.audit_service import AuditService
from app.services.education.education_field_family_service import PAIRABLE_FIELDS, EducationFieldFamilyService

router = APIRouter(prefix="/education-fields", tags=["Education Fields"])


def get_education_field_family_service(
    db: Session = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
) -> EducationFieldFamilyService:
    return EducationFieldFamilyService(EducationFieldRelationRepository(db), audit_service)


@router.get(
    "/relations",
    response_model=APIResponse[EducationFieldRelationList],
    status_code=status.HTTP_200_OK,
    summary="Related education fields per role domain (HR_ADMIN)",
)
def list_education_field_relations(
    service: EducationFieldFamilyService = Depends(get_education_field_family_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    relations = [EducationFieldRelationItem.model_validate(r) for r in service.list_all()]
    return APIResponse.ok(data=EducationFieldRelationList(
        relations=relations, fields=list(PAIRABLE_FIELDS), role_domains=[d.value for d in RoleDomain],
    ), message="Related education fields retrieved.")


@router.post(
    "/relations",
    response_model=APIResponse[EducationFieldRelationItem],
    status_code=status.HTTP_200_OK,
    summary="Relate two fields of study for a role domain (HR_ADMIN)",
    description="Used by the education gate on JDs of that role domain whose education is mandatory.",
)
def add_education_field_relation(
    request: EducationFieldRelationCreate,
    service: EducationFieldFamilyService = Depends(get_education_field_family_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    relation = service.add(field_1=request.field_1, field_2=request.field_2, role_domain=request.role_domain,
                           note=request.note, actor_id=user.user_id, actor_role=authorising_role(user))
    return APIResponse.ok(data=EducationFieldRelationItem.model_validate(relation), message="Related fields added.")


@router.delete(
    "/relations/{relation_id}",
    response_model=APIResponse[None],
    status_code=status.HTTP_200_OK,
    summary="Remove a related-field pair (HR_ADMIN)",
)
def remove_education_field_relation(
    relation_id: UUID,
    service: EducationFieldFamilyService = Depends(get_education_field_family_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    service.remove(relation_id, actor_id=user.user_id, actor_role=authorising_role(user))
    return APIResponse.ok(message="Related fields removed.")
