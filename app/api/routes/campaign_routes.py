from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Security, status

from app.dependencies.campaign import get_campaign_service, get_upload_history_service
from app.enums.constants import MAX_PAGE_SIZE
from app.models.identity import UserRole
from app.schemas.campaign.campaign_response import CampaignResponse, CampaignScoringConfigurationResponse, CampaignScoringDefaultsResponse, CampaignWeightHistoryResponse, HiringCampaignResponse, CampaignMinimalResponse, CampaignPageResponse
from app.schemas.campaign.campaign_schema import CampaignCreateRequest, CampaignUpdateRequest, PlatformDefaultWeightsUpdateRequest
from app.schemas.campaign.campaign_detail_response import CampaignDetailResponse
from app.schemas.campaign.pipeline_summary_response import PipelineSummaryResponse
from app.schemas.campaign.campaign_processing_status_response import (ProcessingStatusSummaryResponse,
    DeadLetterQueueEntryResponse,
)
from app.schemas.campaign.campaign_processing_queue_response import (ProcessingQueueResponse,
    DLQReplayRequest,
    DLQReplayResponse,
)
from app.schemas.campaign.campaign_monitoring_schema import (StalledCandidatesResponse,
    StalledActionResponse,
    StageOverrideRequest,
    FlagReviewRequest,
    EscalateStallRequest,
    RejectionAnalyticsResponse,
)
from app.schemas.campaign.campaign_timeline_response import CampaignTimelineResponse
from app.schemas.campaign.campaign_weight_preset_schema import CampaignWeightPresetCreateRequest, CampaignWeightPresetResponse, CampaignWeightPresetUpdateRequest
from app.schemas.campaign.campaign_pause_schema import PauseImpactSummaryResponse, ResumeSummaryResponse
from app.schemas.campaign.campaign_closure_schema import (CampaignCloseRequest,
    CampaignClosureImpactSummaryResponse,
    CampaignClosureResultResponse,
)
from app.schemas.campaign.campaign_reopen_schema import (CampaignReopenReadinessResponse,
    CampaignReopenResultResponse,
)
from app.schemas.response import APIResponse
from app.schemas.upload_history.response import UnifiedUploadHistoryResponse
from app.services.campaign.campaign_service import CampaignService
from app.services.upload_history.upload_history_service import UploadHistoryService
from app.middleware.rbac import TokenUser, require_roles
from app.schemas.campaign.campaign_filter_schema import CampaignFilterRequest
from app.models.campaigns import CampaignStatus


router = APIRouter(prefix="/campaigns",
    tags=["Campaigns"],
)

SYSTEM_ORG = UUID("11111111-1111-1111-1111-111111111111")


@router.post("",
    response_model=APIResponse[CampaignResponse],
    status_code=status.HTTP_201_CREATED,
)
def create_campaign(request: CampaignCreateRequest,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    org_id = SYSTEM_ORG
    created_by = user.user_id

    campaign = service.create_campaign(request=request,
        org_id=org_id,
        created_by=created_by
    )

    return APIResponse.ok(data=campaign,
        message="Campaign created successfully"
    )

@router.get("/active",
    response_model=APIResponse[list[CampaignMinimalResponse]],
    status_code=status.HTTP_200_OK,
    summary="Get all active campaigns (id + name only)",
    description="Lightweight list of ACTIVE campaigns for dropdowns/pickers.",
)
def get_active_campaigns(service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    return APIResponse.ok(data=service.get_active_campaigns_minimal(),
        message="Active campaigns retrieved successfully",
    )

@router.get("/all",
    response_model=APIResponse[list[CampaignResponse]],
    status_code=status.HTTP_200_OK,
    summary="Get all campaigns",
    description="Retrieve a list of all campaigns with JD and hiring manager details.",
)
def get_all_campaigns(search: str | None = Query(None),
    status: CampaignStatus | None = Query(None),
    hiring_manager_id: str | None = Query(None),
    jd_id: UUID | None = Query(None),
    has_deadline: bool | None = Query(None),
    show_closed: bool = Query(False),
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
):
    filters = CampaignFilterRequest(search=search,
        status=status,
        hiring_manager_id=hiring_manager_id,
        jd_id=jd_id,
        has_deadline=has_deadline,
        show_closed=show_closed,
    )


    campaigns = service.search_campaigns(filters, requesting_user=user)

    return APIResponse.ok(data=campaigns,
        message="Campaigns retrieved successfully",
    )

@router.get("/hr_admin",
    response_model=APIResponse[CampaignPageResponse],
    status_code=status.HTTP_200_OK,
    summary="Get campaigns created by the requesting HR_ADMIN",
    description="Retrieve campaigns created by the requesting HR_ADMIN, paginated 6 per page.",
)
def get_campaigns_by_manager(show_closed: bool = Query(False),
    search: str | None = Query(None),
    status: CampaignStatus | None = Query(None),
    page: int = Query(default=1, ge=1),
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    campaigns = service.get_all_campaigns_for_hrAdmin(
        created_by=user.user_id, show_closed=show_closed, search=search, status=status,
        page=page, page_size=6,
    )

    return APIResponse.ok(data=campaigns,
        message="Campaigns retrieved successfully"
    )

@router.get("/hiring_manager",
    response_model=APIResponse[list[CampaignResponse]],
    status_code=status.HTTP_200_OK,
    summary="Get campaigns by hiring manager ID",
    description="Retrieve a list of campaigns by hiring manager ID with JD and hiring manager details.",
)
def get_campaigns_by_hiring_manager(show_closed: bool = Query(False),
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HIRING_MANAGER)),
):
    campaigns = service.get_all_campaigns_for_hiring_manager(user.user_id, show_closed=show_closed)

    return APIResponse.ok(data=campaigns,
        message="Campaigns retrieved successfully"
    )

# ── Pause an Active Campaign ──────────────────────────────────────────

@router.get("/{campaign_id}/pause-summary",
    response_model=APIResponse[PauseImpactSummaryResponse],
    status_code=status.HTTP_200_OK,
    summary="Pause impact summary",
    description="Impact summary shown in the pause confirmation dialog (HR_ADMIN).",
)
def get_pause_summary(campaign_id: UUID,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    return APIResponse.ok(data=service.get_pause_impact_summary(campaign_id),
        message="Pause impact summary retrieved successfully",
    )


# ── Resume a Paused Campaign ──────────────────────────────────────────

@router.get("/{campaign_id}/resume-summary",
    response_model=APIResponse[ResumeSummaryResponse],
    status_code=status.HTTP_200_OK,
    summary="Resume queued-task summary",
    description="Summary shown in the resume confirmation dialog (HR_ADMIN).",
)
def get_resume_summary(campaign_id: UUID,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    return APIResponse.ok(data=service.get_resume_summary(campaign_id),
        message="Resume summary retrieved successfully",
    )

# ── Close a Campaign Manually ─────────────────────────────────────────

@router.get("/{campaign_id}/closure-summary",
    response_model=APIResponse[CampaignClosureImpactSummaryResponse],
    status_code=status.HTTP_200_OK,
    summary="Closure impact summary",
    description="Impact summary shown in the close confirmation dialog (HR_ADMIN).",
)
def get_closure_summary(campaign_id: UUID,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    return APIResponse.ok(data=service.get_closure_impact_summary(campaign_id),
        message="Closure impact summary retrieved successfully",
    )

@router.post("/{campaign_id}/close",
    response_model=APIResponse[CampaignClosureResultResponse],
    status_code=status.HTTP_200_OK,
    summary="Manually close a campaign",
    description="Terminal closure — cancels in-flight processing and uploads, then returns the closure summary.",
)
def close_campaign(campaign_id: UUID,
    request: CampaignCloseRequest,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    result = service.close_campaign(campaign_id, request, updated_by=user.user_id)
    return APIResponse.ok(data=result, message="Campaign closed successfully")

# ── Reopen a Closed Campaign ──────────────────────────────────────────

@router.get("/{campaign_id}/reopen-readiness",
    response_model=APIResponse[CampaignReopenReadinessResponse],
    status_code=status.HTTP_200_OK,
    summary="Reopen readiness check",
    description="JD/skill readiness validation + current config, for the reopen confirmation dialog (HR_ADMIN).",
)
def get_reopen_readiness(campaign_id: UUID,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    return APIResponse.ok(data=service.get_reopen_readiness(campaign_id),
        message="Reopen readiness retrieved successfully",
    )

@router.post("/{campaign_id}/reopen",
    response_model=APIResponse[CampaignReopenResultResponse],
    status_code=status.HTTP_200_OK,
    summary="Reopen a closed campaign",
    description="Re-validates JD readiness, restores ACTIVE status, clears an already-passed deadline, and records CAMPAIGN_REOPENED.",
)
def reopen_campaign(campaign_id: UUID,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    result = service.reopen_campaign(campaign_id, updated_by=user.user_id)
    return APIResponse.ok(data=result, message="Campaign reopened successfully")

@router.get("/scoring-presets",
    response_model=list[CampaignWeightPresetResponse],
    status_code=status.HTTP_200_OK,
    summary="Get Campaign Weight Presets",
    description="Returns system presets and organization custom presets.",
)
def get_weight_presets(service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):

    return service.get_weight_presets(org_id=SYSTEM_ORG,
    )

# ── Reset Weights to Platform Defaults ────────────────────────────────

@router.get("/platform-defaults/scoring",
    response_model=APIResponse[CampaignScoringDefaultsResponse],
    status_code=status.HTTP_200_OK,
    summary="Get platform default scoring weights",
    description=("Returns the org-wide scoring defaults used to prefill new campaigns "
        "and by the Reset to Defaults option."
    ),
)
def get_platform_default_weights(service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    defaults = service.get_platform_scoring_defaults()
    return APIResponse.ok(data=defaults, message="Platform default scoring weights retrieved successfully")


@router.put("/platform-defaults/scoring",
    response_model=APIResponse[CampaignScoringDefaultsResponse],
    status_code=status.HTTP_200_OK,
    summary="Update platform default scoring weights",
    description=("Updates the org-wide scoring defaults used by new campaigns and the "
        "Reset to Defaults option. Existing campaigns are not affected."
    ),
)
def update_platform_default_weights(request: PlatformDefaultWeightsUpdateRequest,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    defaults = service.update_platform_default_weights(request, updated_by=user.user_id)
    return APIResponse.ok(data=defaults, message="Platform default scoring weights updated successfully")


@router.get("/{campaign_id}/scoring-config",
    response_model=APIResponse[CampaignScoringConfigurationResponse],
    status_code=status.HTTP_200_OK,
    summary="Get campaign scoring configuration",
    description="Retrieve the scoring weights and thresholds configured for a campaign.",
)
def get_scoring_configuration(campaign_id: UUID,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    scoring_config = service.get_scoring_configuration(campaign_id)

    return APIResponse.ok(data=scoring_config,
        message="Campaign scoring configuration retrieved successfully",
    )

@router.get("/{campaign_id}/scoring-history",
    response_model=APIResponse[CampaignWeightHistoryResponse],
)
def get_scoring_history(campaign_id: UUID,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):

    history = service.get_scoring_history(campaign_id)

    return APIResponse.ok(data=history,
        message="Scoring history retrieved successfully",
    )

@router.post("/{campaign_id}/scoring-config/reset",
    response_model=APIResponse[CampaignScoringConfigurationResponse],
    status_code=status.HTTP_200_OK,
    summary="Reset scoring config to platform defaults",
    description="Resets weight_deterministic/semantic/ai and semantic_threshold/ai_threshold to the current platform defaults.",
)
def reset_scoring_configuration(campaign_id: UUID,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    result = service.reset_scoring_to_defaults(campaign_id, updated_by=user.user_id)
    return APIResponse.ok(data=result, message="Scoring configuration reset to platform defaults")

@router.post("/scoring-presets",
    response_model=CampaignWeightPresetResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Campaign Weight Preset",
    description="Creates a custom campaign scoring weight preset.",
)
def create_weight_preset(request: CampaignWeightPresetCreateRequest,
    service: CampaignService = Depends(get_campaign_service),
    current_user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    return service.create_weight_preset(request=request,
        org_id=SYSTEM_ORG,
        created_by=current_user.user_id,
    )

@router.put("/scoring-presets/{preset_id}",
    response_model=CampaignWeightPresetResponse,
    status_code=status.HTTP_200_OK,
    summary="Update Campaign Weight Preset",
)
def update_weight_preset(preset_id: UUID,
    request: CampaignWeightPresetUpdateRequest,
    service: CampaignService = Depends(get_campaign_service),
    current_user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):

    return service.update_weight_preset(preset_id=preset_id,
        request=request,
        org_id=SYSTEM_ORG,
        updated_by=current_user.user_id,
    )

@router.delete("/scoring-presets/{preset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete Campaign Weight Preset",
)
def delete_weight_preset(preset_id: UUID,
    service: CampaignService = Depends(get_campaign_service),
    current_user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):

    service.delete_weight_preset(preset_id=preset_id,
        org_id=SYSTEM_ORG,
        deleted_by=current_user.user_id,
    )

@router.get("/{campaign_id}/details",
    response_model=APIResponse[CampaignDetailResponse],
    status_code=status.HTTP_200_OK,
    summary="Get campaign details by ID",
    description="Retrieve detailed information about a specific campaign.",
)
def get_campaign_details(campaign_id: UUID,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.HIRING_MANAGER, UserRole.RECRUITER)),
):
    campaign_details = service.get_campaign_details(campaign_id, user)

    return APIResponse.ok(data=campaign_details,
        message="Campaign details retrieved successfully"
    )


@router.get("/{campaign_id}/pipeline-summary",
    response_model=APIResponse[PipelineSummaryResponse],
    status_code=status.HTTP_200_OK,
    summary="Get campaign pipeline funnel summary",
    description="Candidate counts per pipeline stage with drop-off percentages.",
)
def get_pipeline_summary(campaign_id: UUID,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    summary = service.get_pipeline_summary(campaign_id)
    return APIResponse.ok(data=summary, message="Pipeline summary retrieved successfully.")


@router.get(
    "/{campaign_id}/upload-history",
    response_model=APIResponse[UnifiedUploadHistoryResponse],
    status_code=status.HTTP_200_OK,
    summary="Get Unified Upload History",
    description=(
        "Epic 4 (M05-E04) Phase D7 - individual resume uploads and bulk "
        "ZIP uploads for a campaign, combined into one chronological, "
        "filterable view."
    ),
)
def get_campaign_upload_history(
    campaign_id: UUID,
    uploaded_by: str | None = Query(default=None),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    upload_type: str | None = Query(default=None, description="individual or bulk"),
    outcome: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    service: UploadHistoryService = Depends(get_upload_history_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    history = service.get_history(
        campaign_id,
        uploaded_by=uploaded_by,
        date_from=date_from,
        date_to=date_to,
        upload_type=upload_type,
        outcome=outcome,
        limit=limit,
        offset=offset,
    )
    return APIResponse.ok(data=history, message="Upload history retrieved successfully.")


@router.get("/{campaign_id}/processing-status",
    response_model=APIResponse[ProcessingStatusSummaryResponse],
    status_code=status.HTTP_200_OK,
    summary="Processing status summary",
    description="celery_task_log status breakdown (QUEUED/RUNNING/RETRY/DEAD/PAUSED) + dead_letter_queue count for this campaign.",
)
def get_processing_status(campaign_id: UUID,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    summary = service.get_processing_status_summary(campaign_id)
    return APIResponse.ok(data=summary, message="Processing status summary retrieved successfully.")


@router.get("/{campaign_id}/dead-letter-queue",
    response_model=APIResponse[list[DeadLetterQueueEntryResponse]],
    status_code=status.HTTP_200_OK,
    summary="Dead letter queue entries for this campaign",
    description="Destination for the DEAD metric card click-through.",
)
def get_dead_letter_queue(campaign_id: UUID,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    entries = service.get_dead_letter_queue_for_campaign(campaign_id)
    return APIResponse.ok(data=entries, message="Dead letter queue entries retrieved successfully.")


# ── View Campaign Processing Queue Status ─────────────────────────────

@router.get("/{campaign_id}/processing-queue",
    response_model=APIResponse[ProcessingQueueResponse],
    status_code=status.HTTP_200_OK,
    summary="Processing queue breakdown by task type",
    description=("Per-task-type status counts, average duration, cumulative LLM token "
        "usage, circuit-breaker states, and estimated completion time."
    ),
)
def get_processing_queue(campaign_id: UUID,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    return APIResponse.ok(data=service.get_processing_queue(campaign_id),
        message="Processing queue retrieved successfully.",
    )


@router.post("/{campaign_id}/dead-letter-queue/replay",
    response_model=APIResponse[DLQReplayResponse],
    status_code=status.HTTP_200_OK,
    summary="Replay selected dead-letter-queue tasks",
    description=("Re-enqueues the selected DLQ entries under fresh task ids, with "
        "per-entry outcomes. A replay limit from platform_config "
        "(MAX_DLQ_REPLAYS_PER_TASK) blocks infinite replay loops."
    ),
)
def replay_dead_letter_tasks(campaign_id: UUID,
    request: DLQReplayRequest,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    result = service.replay_dead_letter_tasks(campaign_id,
        request.dlq_ids,
        replayed_by=user.user_id,
        actor_role=user.roles[0] if user.roles else None,
    )
    return APIResponse.ok(data=result, message="Replay completed.")


# ── Identify & Action Stalled Candidates ──────────────────────────────

@router.get("/{campaign_id}/stalled-candidates",
    response_model=APIResponse[StalledCandidatesResponse],
    status_code=status.HTTP_200_OK,
    summary="Stalled candidates for this campaign",
    description="Candidates stuck in SCREENING/HM_REVIEW/INTERVIEW past their platform_config SLA, with stall reason and last actor.",
)
def get_stalled_candidates(campaign_id: UUID,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    return APIResponse.ok(data=service.get_stalled_candidates(campaign_id),
        message="Stalled candidates retrieved successfully.",
    )


@router.post("/{campaign_id}/stalled-candidates/{campaign_candidate_id}/reprocess",
    response_model=APIResponse[StalledActionResponse],
    status_code=status.HTTP_200_OK,
    summary="Re-process a stalled candidate's failed tasks",
    description="Replays the candidate's dead-lettered tasks via the S03 replay engine (same limits and audit trail).",
)
def reprocess_stalled_candidate(campaign_id: UUID,
    campaign_candidate_id: UUID,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    result = service.reprocess_stalled_candidate(campaign_id, campaign_candidate_id,
        actor_id=user.user_id, actor_role=user.roles[0] if user.roles else None,
    )
    return APIResponse.ok(data=result, message="Re-process completed.")


@router.post("/{campaign_id}/stalled-candidates/{campaign_candidate_id}/escalate",
    response_model=APIResponse[StalledActionResponse],
    status_code=status.HTTP_200_OK,
    summary="Escalate an HM_REVIEW stall to the hiring manager",
)
def escalate_stalled_candidate(campaign_id: UUID,
    campaign_candidate_id: UUID,
    request: EscalateStallRequest,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    result = service.escalate_stalled_candidate(campaign_id, campaign_candidate_id, request,
        actor_id=user.user_id, actor_role=user.roles[0] if user.roles else None,
    )
    return APIResponse.ok(data=result, message="Escalation recorded.")


@router.post("/{campaign_id}/stalled-candidates/{campaign_candidate_id}/override-stage",
    response_model=APIResponse[StalledActionResponse],
    status_code=status.HTTP_200_OK,
    summary="Manually advance a stalled candidate's pipeline stage",
    description="Mandatory reason; records a stage-history row with transition_source=OVERRIDE plus an audit entry.",
)
def override_candidate_stage(campaign_id: UUID,
    campaign_candidate_id: UUID,
    request: StageOverrideRequest,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    result = service.override_candidate_stage(campaign_id, campaign_candidate_id, request,
        actor_id=user.user_id, actor_role=user.roles[0] if user.roles else None,
    )
    return APIResponse.ok(data=result, message="Stage overridden successfully.")


@router.post("/{campaign_id}/stalled-candidates/{campaign_candidate_id}/flag-review",
    response_model=APIResponse[StalledActionResponse],
    status_code=status.HTTP_200_OK,
    summary="Flag a stalled candidate for manual (fraud) review",
)
def flag_candidate_for_review(campaign_id: UUID,
    campaign_candidate_id: UUID,
    request: FlagReviewRequest,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    result = service.flag_candidate_for_review(campaign_id, campaign_candidate_id, request,
        actor_id=user.user_id, actor_role=user.roles[0] if user.roles else None,
    )
    return APIResponse.ok(data=result, message="Candidate flagged for manual review.")


# ── Campaign Rejection Analytics ──────────────────────────────────────

@router.get("/{campaign_id}/rejection-analytics",
    response_model=APIResponse[RejectionAnalyticsResponse],
    status_code=status.HTTP_200_OK,
    summary="Rejection breakdown by layer and reason",
    description="Layer counts, top-10 reasons, missing-mandatory-skill analysis, and threshold-based recommendations.",
)
def get_rejection_analytics(campaign_id: UUID,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    return APIResponse.ok(data=service.get_rejection_analytics(campaign_id),
        message="Rejection analytics retrieved successfully.",
    )



@router.get("/{campaign_id}/timeline",
    response_model=APIResponse[CampaignTimelineResponse],
    status_code=status.HTTP_200_OK,
    summary="Get campaign activity timeline",
    description="Chronological feed of campaign events merged from the audit log and candidate stage history.",
)
def get_campaign_timeline(campaign_id: UUID,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    event_type: str | None = Query(default=None),
):
    timeline = service.get_campaign_timeline(campaign_id=campaign_id,
        limit=limit,
        offset=offset,
        event_type=event_type,
    )
    return APIResponse.ok(data=timeline, message="Campaign timeline retrieved successfully.")


@router.patch("/{campaign_id}",
    response_model=APIResponse[CampaignResponse],
    status_code=status.HTTP_200_OK,
    summary="Edit campaign configuration",
    description=("Update name, deadline, candidate cap, or scoring configuration. "
        "Closed campaigns are read-only. Scoring changes on an ACTIVE campaign "
        "require confirm_scoring_change=true."
    ),
)
def update_campaign(campaign_id: UUID,
    request: CampaignUpdateRequest,
    service: CampaignService = Depends(get_campaign_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    campaign = service.update_campaign(campaign_id=campaign_id,
        request=request,
        updated_by=user.user_id,
    )
    return APIResponse.ok(data=campaign, message="Campaign updated successfully.")   
    