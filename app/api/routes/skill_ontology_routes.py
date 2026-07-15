from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Security, UploadFile, status

from app.dependencies.skill_ontology import get_skill_ontology_service
from app.middleware.rbac import TokenUser, require_roles
from app.models.identity import UserRole
from app.schemas.response import APIResponse
from app.schemas.skill_ontology.skill_ontology_request import (
    SkillCreateRequest,
    SkillOntologyUpdateRequest,
    SkillStatusUpdateRequest,
)
from app.schemas.skill_ontology.skill_ontology_response import (
    BulkImportResponse,
    BulkImportValidationResponse,
    ParentSkillResponse,
    SkillCategoryResponse,
    SkillCreateResponse,
    SkillDeactivationImpactResponse,
    SkillHierarchyNodeResponse,
    SkillOntologyPageResponse,
    SkillOntologyResponse,
    SkillOntologySummaryResponse,
)
from app.services.skills.SkillOntologyService import SkillOntologyService

router = APIRouter(prefix="/skill-ontology", tags=["Skill Ontology"])


@router.get(
    "/summary",
    response_model=APIResponse[SkillOntologySummaryResponse],
    status_code=status.HTTP_200_OK,
)
def get_skill_ontology_summary(
    service: SkillOntologyService = Depends(get_skill_ontology_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
):
    summary = service.get_dashboard_summary()
    return APIResponse.ok(data=summary, message="Skill ontology summary retrieved successfully.")


@router.get(
    "/categories",
    response_model=APIResponse[list[SkillCategoryResponse]],
    status_code=status.HTTP_200_OK,
)
def get_skill_ontology_categories(
    service: SkillOntologyService = Depends(get_skill_ontology_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
):
    categories = service.get_categories()
    return APIResponse.ok(data=categories, message="Skill ontology categories retrieved successfully.")


@router.get(
    "/export",
    status_code=status.HTTP_200_OK,
)
def export_skill_ontology(
    service: SkillOntologyService = Depends(get_skill_ontology_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
    search: Optional[str] = Query(default=None),
    category: Optional[str] = Query(default=None),
    confidence: Optional[str] = Query(default=None),
    is_active: Optional[bool] = Query(default=None),
):
    return service.export_skills(
        search=search,
        category=category,
        confidence=confidence,
        is_active=is_active,
    )


@router.get(
    "/parents",
    response_model=APIResponse[list[ParentSkillResponse]],
    status_code=status.HTTP_200_OK,
)
def search_parent_skills(
    service: SkillOntologyService = Depends(get_skill_ontology_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
    search: Optional[str] = Query(default=None),
    exclude_skill_id: Optional[UUID] = Query(
        default=None,
        description="Skill being edited — excludes it and all of its descendants from the results.",
    ),
):
    parents = service.get_parents(search=search, exclude_skill_id=exclude_skill_id)
    return APIResponse.ok(data=parents, message="Parent skills retrieved successfully.")


@router.get(
    "/hierarchy",
    response_model=APIResponse[list[SkillHierarchyNodeResponse]],
    status_code=status.HTTP_200_OK,
)
def get_skill_hierarchy_roots(
    service: SkillOntologyService = Depends(get_skill_ontology_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
):
    """Root skills only (parent_skill_id IS NULL) — the tree's top level. Call GET /{skill_id}/children to expand a node."""
    roots = service.get_hierarchy_roots()
    return APIResponse.ok(data=roots, message="Skill hierarchy root skills retrieved successfully.")


@router.post(
    "/import/validate",
    response_model=APIResponse[BulkImportValidationResponse],
    status_code=status.HTTP_200_OK,
)
def validate_bulk_import_skill_ontology(
    file: UploadFile = File(...),
    service: SkillOntologyService = Depends(get_skill_ontology_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """S07-T01: dry-run only — never writes to the database. Call this before POST /import."""
    result = service.validate_bulk_import(file)
    return APIResponse.ok(data=result, message="Skill ontology bulk import validation completed.")


@router.post(
    "/import",
    response_model=APIResponse[BulkImportResponse],
    status_code=status.HTTP_200_OK,
)
def bulk_import_skill_ontology(
    file: UploadFile = File(...),
    service: SkillOntologyService = Depends(get_skill_ontology_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    result = service.bulk_import(
        file,
        updated_by=user.user_id,
        actor_role=user.roles[0] if user.roles else None,
    )
    return APIResponse.ok(data=result, message="Skill ontology bulk import completed.")


@router.get(
    "/import/errors/{import_id}",
    status_code=status.HTTP_200_OK,
    responses={
        200: {"content": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {}}, "description": "Excel File"},
        404: {"description": "Import report not found"},
    },
)
def get_bulk_import_error_report(
    import_id: UUID,
    service: SkillOntologyService = Depends(get_skill_ontology_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """S07-T03: downloads the failed-rows Excel report for a completed bulk import (only present when that import had failures)."""
    return service.get_import_error_report(import_id)


@router.get(
    "",
    response_model=APIResponse[SkillOntologyPageResponse],
    status_code=status.HTTP_200_OK,
)
def list_skill_ontology(
    service: SkillOntologyService = Depends(get_skill_ontology_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    search: Optional[str] = Query(default=None),
    category: Optional[str] = Query(default=None),
    confidence: Optional[str] = Query(default=None),
    is_active: Optional[bool] = Query(default=None),
):
    result = service.get_skills(
        page=page,
        page_size=page_size,
        search=search,
        category=category,
        confidence=confidence,
        is_active=is_active,
    )
    return APIResponse.ok(data=result, message="Skill ontology list retrieved successfully.")


@router.post(
    "",
    response_model=APIResponse[SkillCreateResponse],
    status_code=status.HTTP_201_CREATED,
)
def create_skill_ontology(
    request: SkillCreateRequest,
    service: SkillOntologyService = Depends(get_skill_ontology_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    created = service.create_skill(request)
    return APIResponse.ok(data=created, message="Skill created successfully.")


@router.get(
    "/{skill_id}",
    response_model=APIResponse[SkillOntologyResponse],
    status_code=status.HTTP_200_OK,
)
def get_skill_ontology_detail(
    skill_id: UUID,
    service: SkillOntologyService = Depends(get_skill_ontology_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
):
    detail = service.get_skill_detail(skill_id)
    return APIResponse.ok(data=detail, message="Skill ontology detail retrieved successfully.")


@router.patch(
    "/{skill_id}",
    response_model=APIResponse[SkillOntologyResponse],
    status_code=status.HTTP_200_OK,
)
def update_skill_ontology(
    skill_id: UUID,
    request: SkillOntologyUpdateRequest,
    service: SkillOntologyService = Depends(get_skill_ontology_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    updated = service.update_skill(
        skill_id,
        request,
        updated_by=user.user_id,
        actor_role=user.roles[0] if user.roles else None,
    )
    return APIResponse.ok(data=updated, message="Skill updated successfully.")


@router.patch(
    "/{skill_id}/status",
    response_model=APIResponse[SkillOntologyResponse],
    status_code=status.HTTP_200_OK,
)
def update_skill_ontology_status(
    skill_id: UUID,
    request: SkillStatusUpdateRequest,
    service: SkillOntologyService = Depends(get_skill_ontology_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    updated = service.update_status(
        skill_id,
        request,
        updated_by=user.user_id,
        actor_role=user.roles[0] if user.roles else None,
    )
    return APIResponse.ok(data=updated, message="Skill status updated successfully.")


@router.get(
    "/{skill_id}/deactivation-impact",
    response_model=APIResponse[SkillDeactivationImpactResponse],
    status_code=status.HTTP_200_OK,
)
def get_skill_deactivation_impact(
    skill_id: UUID,
    service: SkillOntologyService = Depends(get_skill_ontology_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """
    Read-only preview called before showing the deactivate confirm dialog.
    Never changes is_active — reports candidate_skills/jd_skills usage and
    any immediate children that will need child_handling on the actual
    PATCH /{skill_id}/status call.
    """
    impact = service.get_deactivation_impact(skill_id)
    return APIResponse.ok(data=impact, message="Skill deactivation impact retrieved successfully.")


@router.get(
    "/{skill_id}/children",
    response_model=APIResponse[list[SkillHierarchyNodeResponse]],
    status_code=status.HTTP_200_OK,
)
def get_skill_hierarchy_children(
    skill_id: UUID,
    service: SkillOntologyService = Depends(get_skill_ontology_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
):
    """Immediate children only — never the whole subtree — for expanding one tree node at a time."""
    children = service.get_hierarchy_children(skill_id)
    return APIResponse.ok(data=children, message="Skill children retrieved successfully.")
