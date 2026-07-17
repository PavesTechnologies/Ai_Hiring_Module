from fastapi import APIRouter, Depends, status
from uuid import UUID
from app.dependencies.campaign_candidate import (
    get_campaign_candidate_service,
)

from app.middleware.rbac import TokenUser, get_current_user

from app.schemas.campaign.campaign_candidate_schema import (
    CampaignCandidateCreateRequest,
    CampaignCandidateResponse,
)

from app.schemas.response import APIResponse

from app.services.campaign.campaign_candidate_service import (
    CampaignCandidateService,
)


router = APIRouter(
    prefix="/campaign-candidates",
    tags=["Campaign Candidates"],
)


@router.post(
    "",
    response_model=APIResponse[CampaignCandidateResponse],
    status_code=status.HTTP_201_CREATED,
)
def create_campaign_candidate(
    request: CampaignCandidateCreateRequest,
    service: CampaignCandidateService = Depends(
        get_campaign_candidate_service
    ),
    user: TokenUser = Depends(get_current_user),
):

    candidate = service.create_campaign_candidate(
        request,
        actor_id=user.user_id,
        actor_role=user.roles[0] if user.roles else None,
    )

    return APIResponse.ok(
        data=candidate,
        message="Candidate added to campaign successfully.",
    )

@router.get(
    "/campaign/{campaign_id}",
    response_model=APIResponse[list[CampaignCandidateResponse]],
    status_code=status.HTTP_200_OK,
    summary="Get Campaign Candidates",
    description="Retrieve all candidates belonging to a campaign.",
)
def get_campaign_candidates(
    campaign_id: UUID,
    service: CampaignCandidateService = Depends(
        get_campaign_candidate_service,
    ),
):

    candidates = service.get_campaign_candidates(
        campaign_id
    )

    return APIResponse.ok(
        data=candidates,
        message="Campaign candidates retrieved successfully.",
    )

@router.delete(
    "/{campaign_candidate_id}",
    status_code=status.HTTP_200_OK,
    response_model=APIResponse[None],
    summary="Delete Campaign Candidate",
    description="Delete a candidate from a campaign.",
)
def delete_campaign_candidate(
    campaign_candidate_id: UUID,
    service: CampaignCandidateService = Depends(
        get_campaign_candidate_service,
    ),
    user: TokenUser = Depends(get_current_user),
):
    service.delete_campaign_candidate(
        campaign_candidate_id,
        actor_id=user.user_id,
        actor_role=user.roles[0] if user.roles else None,
    )

    return APIResponse.ok(
        message="Campaign candidate deleted successfully.",
    )