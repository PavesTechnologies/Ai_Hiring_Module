"""JD profile templates (Phase 3 Step 7): Fresher / Experienced / Domain - HR_ADMIN only."""
from fastapi import APIRouter, Depends, Path, Security, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies.campaign import get_audit_service
from app.middleware.rbac import TokenUser, require_roles, resolve_actor_role
from app.models.identity import UserRole
from app.repositories.jd_profile_template_repository import JDProfileTemplateRepository
from app.schemas.jd_profile_template import (
    JDProfileTemplateItem,
    JDProfileTemplateList,
    JDProfileTemplateUpdate,
    JDProfileTemplateUpdateResponse,
)
from app.schemas.response import APIResponse
from app.services.audit_service import AuditService
from app.services.jd.jd_profile_template_service import JDProfileTemplateService
from app.services.jd.jd_profile_templates import EVIDENCE_SECTION_OPTIONS, REQUIRED_EVIDENCE_SECTIONS

router = APIRouter(prefix="/jd-profile-templates", tags=["JD Profile Templates"])


def get_jd_profile_template_service(
    db: Session = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
) -> JDProfileTemplateService:
    return JDProfileTemplateService(JDProfileTemplateRepository(db), audit_service)


@router.get(
    "",
    response_model=APIResponse[JDProfileTemplateList],
    status_code=status.HTTP_200_OK,
    summary="JD profile templates (HR_ADMIN)",
)
def list_jd_profile_templates(
    service: JDProfileTemplateService = Depends(get_jd_profile_template_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    return APIResponse.ok(data=JDProfileTemplateList(
        templates=[JDProfileTemplateItem.model_validate(t) for t in service.list_all()],
        evidence_section_options=list(EVIDENCE_SECTION_OPTIONS),
        required_evidence_sections=sorted(REQUIRED_EVIDENCE_SECTIONS),
    ), message="Profile templates retrieved.")


@router.put(
    "/{key}",
    response_model=APIResponse[JDProfileTemplateUpdateResponse],
    status_code=status.HTTP_200_OK,
    summary="Edit a JD profile template (HR_ADMIN)",
    description="Scoring changes (internship credit, tolerance, evidence sections) mark the scored candidates "
                "of live campaigns whose JD uses the template out of date.",
)
def update_jd_profile_template(
    request: JDProfileTemplateUpdate,
    key: str = Path(..., pattern="^(FRESHER|EXPERIENCED|DOMAIN)$"),
    service: JDProfileTemplateService = Depends(get_jd_profile_template_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    template, changed, marked = service.update(key, request, actor_id=user.user_id,
                                               actor_role=resolve_actor_role(user))
    return APIResponse.ok(data=JDProfileTemplateUpdateResponse(
        template=JDProfileTemplateItem.model_validate(template), changed=changed,
        candidates_marked_out_of_date=marked,
    ), message="Profile template saved." if changed else "No changes.")
