from uuid import UUID

from fastapi import APIRouter, Depends, Security, status

from app.dependencies.campaign_candidate import get_campaign_candidate_service
from app.middleware.rbac import require_roles
from app.models.identity import UserRole
from app.schemas.campaign.campaign_candidate_schema import (
    CandidateCampaignHistoryResponse,
)
from app.schemas.response import APIResponse
from app.services.campaign.campaign_candidate_service import (
    CampaignCandidateService,
)

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
