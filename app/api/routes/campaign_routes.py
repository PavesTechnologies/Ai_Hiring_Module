from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Security, status
from fastapi.responses import StreamingResponse

from app.dependencies.campaign import get_campaign_service
from app.models.identity import UserRole
from app.schemas.campaign.campaign_response import CampaignResponse, CampaignScoringConfigurationResponse, CampaignScoringDefaultsResponse, CampaignWeightHistoryResponse, CopyScoringConfigResponse, HiringCampaignResponse
from app.schemas.campaign.campaign_schema import CampaignCreateRequest, CampaignScoringUpdateRequest, CampaignUpdateRequest, CopyScoringConfigRequest, PlatformDefaultWeightsUpdateRequest
from app.schemas.campaign.campaign_detail_response import CampaignDetailResponse
from app.schemas.campaign.pipeline_summary_response import PipelineSummaryResponse
from app.schemas.campaign.campaign_timeline_response import CampaignTimelineResponse
from app.schemas.campaign.campaign_comparison_response import CampaignComparisonResponse
from app.schemas.campaign.campaign_weight_change_report_response import WeightChangeReportResponse
from app.schemas.campaign.campaign_weight_preset_schema import CampaignWeightPresetCreateRequest, CampaignWeightPresetResponse, CampaignWeightPresetUpdateRequest
from app.schemas.campaign.campaign_pause_schema import PauseImpactSummaryResponse, ResumeSummaryResponse
from app.schemas.campaign.campaign_closure_schema import (
    CampaignCloseRequest,
    CampaignClosureImpactSummaryResponse,
    CampaignClosureResultResponse,
)
from app.schemas.response import APIResponse
from app.services.campaign.campaign_service import CampaignService
from app.middleware.rbac import TokenUser, require_roles
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
)
def get_all_campaigns(
    search: str | None = Query(None),
    status: CampaignStatus | None = Query(None),
    hiring_manager_id: str | None = Query(None),
    jd_id: UUID | None = Query(None),
    has_deadline: bool | None = Query(None),
    show_closed: bool = Query(False),
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
):
    filters = CampaignFilterRequest(
        search=search,
        status=status,
        hiring_manager_id=hiring_manager_id,
        jd_id=jd_id,
        has_deadline=has_deadline,
        show_closed=show_closed,
    )

    campaigns = service.search_campaigns(filters, requesting_user=user)

    return APIResponse.ok(
        data=campaigns,
        message="Campaigns retrieved successfully",
    )

@router.get(
    "/hr_admin",
    response_model=APIResponse[list[CampaignResponse]],
    status_code=status.HTTP_200_OK,
    summary="Get all campaigns (HR_ADMIN)",
    description="Retrieve every campaign in the organisation, with JD and hiring manager details.",
)
def get_campaigns_by_manager(
    show_closed: bool = Query(False),
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    campaigns = service.get_all_campaigns_for_hrAdmin(show_closed=show_closed)

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
)
def get_campaigns_by_hiring_manager(
    show_closed: bool = Query(False),
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HIRING_MANAGER)),
):
    campaigns = service.get_all_campaigns_for_hiring_manager(user.user_id, show_closed=show_closed)

    return APIResponse.ok(
        data=campaigns,
        message="Campaigns retrieved successfully"
    )

# ── S01 — Pause an Active Campaign ──────────────────────────────────────────

@router.get(
    "/{campaign_id}/pause-summary",
    response_model=APIResponse[PauseImpactSummaryResponse],
    status_code=status.HTTP_200_OK,
    summary="Pause impact summary",
    description="Impact summary shown in the pause confirmation dialog (HR_ADMIN).",
)
def get_pause_summary(
    campaign_id: UUID,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    return APIResponse.ok(
        data=service.get_pause_impact_summary(campaign_id),
        message="Pause impact summary retrieved successfully",
    )


# ── S02 — Resume a Paused Campaign ──────────────────────────────────────────

@router.get(
    "/{campaign_id}/resume-summary",
    response_model=APIResponse[ResumeSummaryResponse],
    status_code=status.HTTP_200_OK,
    summary="Resume queued-task summary",
    description="Summary shown in the resume confirmation dialog (HR_ADMIN).",
)
def get_resume_summary(
    campaign_id: UUID,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    return APIResponse.ok(
        data=service.get_resume_summary(campaign_id),
        message="Resume summary retrieved successfully",
    )

# ── S03 — Close a Campaign Manually ─────────────────────────────────────────

@router.get(
    "/{campaign_id}/closure-summary",
    response_model=APIResponse[CampaignClosureImpactSummaryResponse],
    status_code=status.HTTP_200_OK,
    summary="Closure impact summary",
    description="Impact summary shown in the close confirmation dialog (HR_ADMIN).",
)
def get_closure_summary(
    campaign_id: UUID,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    return APIResponse.ok(
        data=service.get_closure_impact_summary(campaign_id),
        message="Closure impact summary retrieved successfully",
    )

@router.post(
    "/{campaign_id}/close",
    response_model=APIResponse[CampaignClosureResultResponse],
    status_code=status.HTTP_200_OK,
    summary="Manually close a campaign",
    description="Terminal closure — cancels in-flight processing and uploads, then returns the closure summary.",
)
def close_campaign(
    campaign_id: UUID,
    request: CampaignCloseRequest,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    result = service.close_campaign(campaign_id, request, updated_by=user.user_id)
    return APIResponse.ok(data=result, message="Campaign closed successfully")

@router.get(
    "/scoring-presets",
    response_model=list[CampaignWeightPresetResponse],
    status_code=status.HTTP_200_OK,
    summary="Get Campaign Weight Presets",
    description="Returns system presets and organization custom presets.",
)
def get_weight_presets(
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):

    return service.get_weight_presets(
        org_id=SYSTEM_ORG,
    )

# Compare Weight Configurations Across Campaigns ────────────────────

@router.get(
    "/compare",
    response_model=APIResponse[CampaignComparisonResponse],
    status_code=status.HTTP_200_OK,
    summary="Compare scoring configs across 2-4 campaigns",
    description="Side-by-side weight/threshold config and score distribution for 2-4 campaigns.",
)
def compare_campaigns(
    campaign_ids: list[UUID] = Query(..., min_length=2, max_length=4),
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    comparison = service.compare_campaigns(campaign_ids)
    return APIResponse.ok(data=comparison, message="Campaign comparison retrieved successfully")


# ── S05 — Reset Weights to Platform Defaults ────────────────────────────────

@router.put(
    "/platform-defaults/scoring",
    response_model=APIResponse[CampaignScoringDefaultsResponse],
    status_code=status.HTTP_200_OK,
    summary="Update platform default scoring weights",
    description=(
        "Updates the org-wide scoring defaults used by new campaigns and the "
        "Reset to Defaults option. Existing campaigns are not affected."
    ),
)
def update_platform_default_weights(
    request: PlatformDefaultWeightsUpdateRequest,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    defaults = service.update_platform_default_weights(request, updated_by=user.user_id)
    return APIResponse.ok(data=defaults, message="Platform default scoring weights updated successfully")


@router.get(
    "/reports/weight-changes",
    response_model=APIResponse[WeightChangeReportResponse],
    status_code=status.HTTP_200_OK,
    summary="Consolidated weight change report",
    description="All scoring weight changes across every campaign, for compliance review.",
)
def get_weight_change_report(
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    campaign_status: CampaignStatus | None = Query(default=None),
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    report = service.get_weight_change_report(date_from, date_to, campaign_status)
    return APIResponse.ok(data=report, message="Weight change report retrieved successfully")


@router.get(
    "/reports/weight-changes/export",
    status_code=status.HTTP_200_OK,
    summary="Export the weight change report as XLSX",
)
def export_weight_change_report(
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    campaign_status: CampaignStatus | None = Query(default=None),
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    excel_file = service.export_weight_change_report_xlsx(date_from, date_to, campaign_status)
    filename = f"Weight_Change_Report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return StreamingResponse(
        excel_file,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/{campaign_id}",
    response_model=APIResponse[CampaignResponse],
    status_code=status.HTTP_200_OK,
)
def get_campaign(
    campaign_id: UUID,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
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
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
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
)
def update_scoring_configuration(
    campaign_id: UUID,
    request: CampaignScoringUpdateRequest,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):

    configuration = service.update_scoring_configuration(
        campaign_id=campaign_id,
        request=request,
        updated_by=user.user_id,
    )

    return configuration

@router.post(
    "/{campaign_id}/scoring-config/copy",
    response_model=APIResponse[CopyScoringConfigResponse],
    status_code=status.HTTP_200_OK,
    summary="Copy scoring config to other campaigns",
    description="Copies this campaign's weights/thresholds onto one or more target campaigns.",
)
def copy_scoring_configuration(
    campaign_id: UUID,
    request: CopyScoringConfigRequest,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    result = service.copy_scoring_configuration(
        source_campaign_id=campaign_id,
        request=request,
        updated_by=user.user_id,
    )
    return APIResponse.ok(data=result, message="Scoring configuration copied successfully")

@router.post(
    "/{campaign_id}/scoring-config/reset",
    response_model=APIResponse[CampaignScoringConfigurationResponse],
    status_code=status.HTTP_200_OK,
    summary="Reset scoring config to platform defaults",
    description="Resets weight_deterministic/semantic/ai and semantic_threshold/ai_threshold to the current platform defaults.",
)
def reset_scoring_configuration(
    campaign_id: UUID,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    result = service.reset_scoring_to_defaults(campaign_id, updated_by=user.user_id)
    return APIResponse.ok(data=result, message="Scoring configuration reset to platform defaults")

@router.post(
    "/scoring-presets",
    response_model=CampaignWeightPresetResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Campaign Weight Preset",
    description="Creates a custom campaign scoring weight preset.",
)
def create_weight_preset(
    request: CampaignWeightPresetCreateRequest,
    service: CampaignService = Depends(get_campaign_service),
    current_user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
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
)
def update_weight_preset(
    preset_id: UUID,
    request: CampaignWeightPresetUpdateRequest,
    service: CampaignService = Depends(get_campaign_service),
    current_user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
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
)
def delete_weight_preset(
    preset_id: UUID,
    service: CampaignService = Depends(get_campaign_service),
    current_user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
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
    