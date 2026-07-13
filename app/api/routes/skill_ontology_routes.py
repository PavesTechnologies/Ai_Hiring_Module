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
    ParentSkillResponse,
    SkillCategoryResponse,
    SkillCreateResponse,
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
):
    parents = service.get_parents(search=search)
    return APIResponse.ok(data=parents, message="Parent skills retrieved successfully.")


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
    result = service.bulk_import(file)
    return APIResponse.ok(data=result, message="Skill ontology bulk import completed.")


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
    updated = service.update_skill(skill_id, request)
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
    updated = service.update_status(skill_id, request)
    return APIResponse.ok(data=updated, message="Skill status updated successfully.")
