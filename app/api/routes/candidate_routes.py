from uuid import UUID

from fastapi import APIRouter, Depends, Query, Security, status

from app.dependencies.campaign_candidate import get_campaign_candidate_service
from app.dependencies.resume import get_candidate_erasure_service
from app.middleware.rbac import TokenUser, require_roles
from app.models.identity import UserRole
from app.schemas.campaign.campaign_candidate_schema import (
    CandidateCampaignHistoryResponse,
)
from app.schemas.response import APIResponse
from app.services.campaign.campaign_candidate_service import (
    CampaignCandidateService,
)
from app.services.compliance.candidate_erasure_service import CandidateErasureService

router = APIRouter(
    prefix="/candidates",
    tags=["Candidates"],
)


@router.get(
    "/{candidate_id}/campaign-history",
    response_model=APIResponse[CandidateCampaignHistoryResponse],
    status_code=status.HTTP_200_OK,
    summary="Get Candidate Cross-Campaign History",
    description=(
        "Epic 3 (M05-E03) Phase C6 - every campaign a candidate has been "
        "submitted to, most recent first, with per-campaign stage, "
        "composite_score, and derived outcome. HR_ADMIN only."
    ),
    dependencies=[Security(require_roles(UserRole.HR_ADMIN))],
)
def get_candidate_campaign_history(
    candidate_id: UUID,
    service: CampaignCandidateService = Depends(
        get_campaign_candidate_service,
    ),
):
    history = service.get_candidate_campaign_history(candidate_id)

    return APIResponse.ok(
        data=history,
        message="Candidate campaign history retrieved successfully.",
    )


@router.delete(
    "/{candidate_id}",
    response_model=APIResponse[None],
    status_code=status.HTTP_200_OK,
    summary="Erase Candidate (GDPR-style right to erasure)",
    description=(
        "Permanently deletes a candidate and every row that references "
        "them - resumes (including the stored files), scores, skills, "
        "embeddings, pipeline/campaign history, consent, and email "
        "notifications - regardless of whether they came from an "
        "individual or a bulk ZIP upload. The only surviving trace is the "
        "audit_log entry this call itself writes. HR_ADMIN only."
    ),
)
def erase_candidate(
    candidate_id: UUID,
    reason: str | None = Query(default=None, max_length=500),
    service: CandidateErasureService = Depends(get_candidate_erasure_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    service.erase_candidate(
        candidate_id=candidate_id,
        actor_id=user.user_id,
        actor_role=user.roles[0] if user.roles else None,
        reason=reason,
    )

    return APIResponse.ok(
        data=None,
        message="Candidate and all associated data erased successfully.",
    )
