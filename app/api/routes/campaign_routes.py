from uuid import UUID

from fastapi import APIRouter, Depends, Query, Security, status

from app.dependencies.campaign import get_campaign_service
from app.models.identity import UserRole
from app.schemas.campaign.campaign_response import CampaignResponse, CampaignScoringConfigurationResponse, CampaignWeightHistoryResponse
from app.schemas.campaign.campaign_schema import CampaignCreateRequest, CampaignScoringUpdateRequest, CampaignUpdateRequest
from app.schemas.campaign.campaign_detail_response import CampaignDetailResponse
from app.schemas.campaign.pipeline_summary_response import PipelineSummaryResponse
from app.schemas.campaign.campaign_timeline_response import CampaignTimelineResponse
from app.schemas.campaign.campaign_weight_preset_schema import CampaignWeightPresetCreateRequest, CampaignWeightPresetResponse, CampaignWeightPresetUpdateRequest
from app.schemas.response import APIResponse
from app.services.campaign.campaign_service import CampaignService
from app.middleware.rbac import TokenUser, require_roles, get_current_user
from app.schemas.campaign.campaign_filter_schema import CampaignFilterRequest
from app.models.campaigns import CampaignStatus


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
    created_by = user.user_id

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
    search: str | None = Query(None),
    status: CampaignStatus | None = Query(None),
    hiring_manager_id: str | None = Query(None),
    jd_id: UUID | None = Query(None),
    has_deadline: bool | None = Query(None),
    show_closed: bool = Query(False),
    service: CampaignService = Depends(get_campaign_service),
):
    filters = CampaignFilterRequest(
        search=search,
        status=status,
        hiring_manager_id=hiring_manager_id,
        jd_id=jd_id,
        has_deadline=has_deadline,
        show_closed=show_closed,
    )

    campaigns = service.search_campaigns(filters)

    return APIResponse.ok(
        data=campaigns,
        message="Campaigns retrieved successfully",
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
    "/scoring-presets",
    response_model=list[CampaignWeightPresetResponse],
    status_code=status.HTTP_200_OK,
    summary="Get Campaign Weight Presets",
    description="Returns system presets and organization custom presets.",
    dependencies=[Security(require_roles(UserRole.HR_ADMIN))]
)
def get_weight_presets(
    service: CampaignService = Depends(get_campaign_service),
):

    return service.get_weight_presets(
        org_id=SYSTEM_ORG,
    )

@router.get(
    "/{campaign_id}",
    response_model=APIResponse[CampaignResponse],
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER))],
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

@router.get(
    "/{campaign_id}/scoring-config",
    response_model=APIResponse[CampaignScoringConfigurationResponse],
    status_code=status.HTTP_200_OK,
    summary="Get campaign scoring configuration",
    description="Retrieve the scoring weights and thresholds configured for a campaign.",
)
def get_scoring_configuration(
    campaign_id: UUID,
    service: CampaignService = Depends(get_campaign_service),
):
    scoring_config = service.get_scoring_configuration(campaign_id)

    return APIResponse.ok(
        data=scoring_config,
        message="Campaign scoring configuration retrieved successfully",
    )

@router.get(
    "/{campaign_id}/scoring-history",
    response_model=APIResponse[CampaignWeightHistoryResponse],
)
def get_scoring_history(
    campaign_id: UUID,
    service: CampaignService = Depends(get_campaign_service),
):

    history = service.get_scoring_history(campaign_id)

    return APIResponse.ok(
        data=history,
        message="Scoring history retrieved successfully",
    )

@router.put(
    "/{campaign_id}/scoring-config",
    response_model=CampaignScoringConfigurationResponse,
    status_code=status.HTTP_200_OK,
    summary="Update Campaign Scoring Configuration",
    description="Update scoring weights and thresholds for a campaign.",
    dependencies=[Security(require_roles(UserRole.HR_ADMIN))]
)
def update_scoring_configuration(
    campaign_id: UUID,
    request: CampaignScoringUpdateRequest,
    service: CampaignService = Depends(get_campaign_service),
):

    configuration = service.update_scoring_configuration(
        campaign_id=campaign_id,
        request=request,
    )

    return configuration

@router.post(
    "/scoring-presets",
    response_model=CampaignWeightPresetResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Campaign Weight Preset",
    description="Creates a custom campaign scoring weight preset.",
    dependencies=[Security(require_roles(UserRole.HR_ADMIN))]
)
def create_weight_preset(
    request: CampaignWeightPresetCreateRequest,
    service: CampaignService = Depends(get_campaign_service),
    current_user: TokenUser = Depends(get_current_user),
):
    return service.create_weight_preset(
        request=request,
        org_id=SYSTEM_ORG,
        created_by=current_user.user_id,
    )

@router.put(
    "/scoring-presets/{preset_id}",
    response_model=CampaignWeightPresetResponse,
    status_code=status.HTTP_200_OK,
    summary="Update Campaign Weight Preset",
    dependencies=[Security(require_roles(UserRole.HR_ADMIN))]
)
def update_weight_preset(
    preset_id: UUID,
    request: CampaignWeightPresetUpdateRequest,
    service: CampaignService = Depends(get_campaign_service),
    current_user: TokenUser = Depends(get_current_user),
):

    return service.update_weight_preset(
        preset_id=preset_id,
        request=request,
        org_id=SYSTEM_ORG,
        updated_by=current_user.user_id,
    )

@router.delete(
    "/scoring-presets/{preset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete Campaign Weight Preset",
    dependencies=[Security(require_roles(UserRole.HR_ADMIN))]
)
def delete_weight_preset(
    preset_id: UUID,
    service: CampaignService = Depends(get_campaign_service),
    current_user: TokenUser = Depends(get_current_user),
):

    service.delete_weight_preset(
        preset_id=preset_id,
        org_id=SYSTEM_ORG,
        deleted_by=current_user.user_id,
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
    