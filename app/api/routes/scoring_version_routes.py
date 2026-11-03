"""
Scoring versions (retire v1, phase D) - HR_ADMIN only: list, create a candidate version, promote it to
current, retire it. Trying one on a campaign is PUT /campaigns/{id}/candidate-scoring-version.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, Security, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.middleware.rbac import TokenUser, authorising_role, require_roles
from app.models.identity import UserRole
from app.repositories.audit_repository import AuditRepository
from app.schemas.campaign.scoring_shadow_schema import ScoringVersionCreateRequest, ScoringVersionResponse
from app.schemas.response import APIResponse
from app.services.audit_service import AuditService
from app.services.campaign.scoring_versions import ScoringVersionService

router = APIRouter(prefix="/scoring-versions", tags=["Scoring Versions"])


def _service(db: Session) -> ScoringVersionService:
    return ScoringVersionService(db, AuditService(AuditRepository(db)))


@router.get(
    "",
    response_model=APIResponse[list[ScoringVersionResponse]],
    summary="Scoring versions: the current one, candidates (and the campaigns trying them), retired ones",
    dependencies=[Security(require_roles(UserRole.HR_ADMIN))],
)
def list_scoring_versions(db: Session = Depends(get_db)):
    return APIResponse.ok(data=[ScoringVersionResponse(**v) for v in _service(db).list_versions()],
                          message="Scoring versions.")


@router.post(
    "",
    response_model=APIResponse[ScoringVersionResponse],
    status_code=status.HTTP_201_CREATED,
    summary="Create a candidate scoring version (configuration and optional confirmation model)",
    description=("configuration: the SCORING_V2_CONFIG shape - only the keys that differ from the defaults. "
                 "Code changes, the prompt included, can't be a version: they go through the golden set. Audited."),
)
def create_scoring_version(
    request: ScoringVersionCreateRequest,
    db: Session = Depends(get_db),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    version = _service(db).create(label=request.label, configuration=request.configuration, model=request.model,
                                  note=request.note, actor_id=user.user_id, actor_role=authorising_role(user))
    return APIResponse.ok(data=ScoringVersionResponse(**version), message="Scoring version created.")


@router.post(
    "/{version_id}/promote",
    response_model=APIResponse[ScoringVersionResponse],
    summary="Make a candidate version current (the current one is retired) - affects every campaign",
    description="From the next scoring on, every campaign is scored and decided with it. Audited.",
)
def promote_scoring_version(
    version_id: UUID,
    db: Session = Depends(get_db),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    version = _service(db).promote(version_id, actor_id=user.user_id, actor_role=authorising_role(user))
    return APIResponse.ok(data=ScoringVersionResponse(**version), message="Scoring version promoted.")


@router.post(
    "/{version_id}/retire",
    response_model=APIResponse[ScoringVersionResponse],
    summary="Retire a candidate version (campaigns trying it stop shadow scoring)",
)
def retire_scoring_version(
    version_id: UUID,
    db: Session = Depends(get_db),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    version = _service(db).retire(version_id, actor_id=user.user_id, actor_role=authorising_role(user))
    return APIResponse.ok(data=ScoringVersionResponse(**version), message="Scoring version retired.")
