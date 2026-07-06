from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.dependencies.campaign import get_campaign_service
from app.schemas.campaign.campaign_response import CampaignResponse
from app.schemas.campaign.campaign_schema import CampaignCreateRequest
from app.schemas.response import APIResponse
from app.services.campaign.campaign_service import CampaignService
from app.middleware.rbac import TokenUser, require_roles, get_current_user
from app.enums.constants import UserRole

router = APIRouter(
    prefix="/campaigns",
    tags=["Campaigns"],
)

SYSTEM_ORG = UUID("11111111-1111-1111-1111-111111111111")


@router.post(
    "",
    response_model=APIResponse[CampaignResponse],
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(UserRole.HR_ADMIN))],
)
def create_campaign(
    request: CampaignCreateRequest,
    service: CampaignService = Depends(get_campaign_service),
):
    org_id = SYSTEM_ORG
    created_by = get_current_user().user_id  # Assuming you have a way to get the current user``

    campaign = service.create_campaign(
        request=request,
        org_id=org_id,
        created_by=created_by
    )

    return APIResponse.ok(
        data=campaign,
        message="Campaign created successfully"
    )


@router.get(
    "/{campaign_id}",
    response_model=APIResponse[CampaignResponse],
    status_code=status.HTTP_200_OK,
    summary="Get campaign by ID",
    description="Retrieve a campaign by its ID with JD and hiring manager details.",
)
def get_campaign(
    campaign_id: UUID,
    service: CampaignService = Depends(get_campaign_service),
):
    campaign = service.get_campaign_by_id(campaign_id)

    return APIResponse.ok(
        data=campaign,
        message="Campaign retrieved successfully"
    )
