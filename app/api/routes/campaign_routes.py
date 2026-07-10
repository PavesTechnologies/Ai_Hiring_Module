from uuid import UUID

from fastapi import APIRouter, Depends, Query, Security, status

from app.dependencies.campaign import get_campaign_service
from app.models.identity import UserRole
from app.middleware.rbac import TokenUser, require_roles
from app.schemas.campaign.campaign_response import CampaignResponse
from app.schemas.campaign.campaign_detail_response import CampaignDetailResponse
from app.schemas.campaign.pipeline_summary_response import PipelineSummaryResponse
from app.schemas.campaign.campaign_timeline_response import CampaignTimelineResponse
from app.schemas.campaign.campaign_schema import CampaignCreateRequest, CampaignUpdateRequest
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
    dependencies=[Security(require_roles(UserRole.HR_ADMIN, UserRole.HIRING_MANAGER))]
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

@router.get("/{campaign_id}/details",
    response_model=APIResponse[CampaignDetailResponse],
    status_code=status.HTTP_200_OK,
    summary="Get campaign details by ID",
    description="Retrieve detailed information about a specific campaign.",
)
def get_campaign_details(
    campaign_id: UUID,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.HIRING_MANAGER, UserRole.RECRUITER)),
):
    campaign_details = service.get_campaign_details(campaign_id, user)

    return APIResponse.ok(
        data=campaign_details,
        message="Campaign details retrieved successfully"
    )


@router.get(
    "/{campaign_id}/pipeline-summary",
    response_model=APIResponse[PipelineSummaryResponse],
    status_code=status.HTTP_200_OK,
    summary="Get campaign pipeline funnel summary",
    description="Candidate counts per pipeline stage with drop-off percentages.",
)
def get_pipeline_summary(
    campaign_id: UUID,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    summary = service.get_pipeline_summary(campaign_id)
    return APIResponse.ok(data=summary, message="Pipeline summary retrieved successfully.")


@router.get(
    "/{campaign_id}/timeline",
    response_model=APIResponse[CampaignTimelineResponse],
    status_code=status.HTTP_200_OK,
    summary="Get campaign activity timeline",
    description="Chronological feed of campaign events merged from the audit log and candidate stage history.",
)
def get_campaign_timeline(
    campaign_id: UUID,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    event_type: str | None = Query(default=None),
):
    timeline = service.get_campaign_timeline(
        campaign_id=campaign_id,
        limit=limit,
        offset=offset,
        event_type=event_type,
    )
    return APIResponse.ok(data=timeline, message="Campaign timeline retrieved successfully.")


@router.patch(
    "/{campaign_id}",
    response_model=APIResponse[CampaignResponse],
    status_code=status.HTTP_200_OK,
    summary="Edit campaign configuration",
    description=(
        "Update name, deadline, candidate cap, or scoring configuration. "
        "Closed campaigns are read-only. Scoring changes on an ACTIVE campaign "
        "require confirm_scoring_change=true."
    ),
)
def update_campaign(
    campaign_id: UUID,
    request: CampaignUpdateRequest,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    campaign = service.update_campaign(
        campaign_id=campaign_id,
        request=request,
        updated_by=user.user_id,
    )
    return APIResponse.ok(data=campaign, message="Campaign updated successfully.")   
    