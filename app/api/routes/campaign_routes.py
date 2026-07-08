from uuid import UUID

from fastapi import APIRouter, Depends, Security, status

from app.dependencies.campaign import get_campaign_service
from app.models.identity import UserRole
from app.middleware.rbac import TokenUser, require_roles
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
)
def create_campaign(
    request: CampaignCreateRequest,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    org_id = SYSTEM_ORG
    created_by = user.user_id  # Assuming you have a way to get the current user``

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
    "/all",
    response_model=APIResponse[list[CampaignResponse]],
    status_code=status.HTTP_200_OK,
    summary="Get all campaigns",
    description="Retrieve a list of all campaigns with JD and hiring manager details.",
    dependencies=[Security(require_roles(UserRole.HIRING_MANAGER))]
)
def get_all_campaigns(
    service: CampaignService = Depends(get_campaign_service),
):
    campaigns = service.get_all_campaigns()

    return APIResponse.ok(
        data=campaigns,
        message="Campaigns retrieved successfully"
    )


@router.get(
    "/hr_admin",
    response_model=APIResponse[list[CampaignResponse]],
    status_code=status.HTTP_200_OK,
    summary="Get campaigns by manager ID",
    description="Retrieve a list of campaigns by manager ID with JD and hiring manager details.",
    dependencies=[Security(require_roles(UserRole.HR_ADMIN))]
)
def get_campaigns_by_manager(
    user: TokenUser = Depends(get_current_user),
    service: CampaignService = Depends(get_campaign_service),
):
    campaigns = service.get_all_campaigns_for_hrAdmin(user.user_id)

    return APIResponse.ok(
        data=campaigns,
        message="Campaigns retrieved successfully"
    )

@router.get(
    "/hiring_manager",
    response_model=APIResponse[list[CampaignResponse]],
    status_code=status.HTTP_200_OK,
    summary="Get campaigns by hiring manager ID",
    description="Retrieve a list of campaigns by hiring manager ID with JD and hiring manager details.",
    dependencies=[Security(require_roles(UserRole.HIRING_MANAGER))]
)
def get_campaigns_by_hiring_manager(
    user: TokenUser = Depends(get_current_user),
    service: CampaignService = Depends(get_campaign_service),
):
    campaigns = service.get_all_campaigns_for_hiring_manager(user.user_id)

    return APIResponse.ok(
        data=campaigns,
        message="Campaigns retrieved successfully"
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