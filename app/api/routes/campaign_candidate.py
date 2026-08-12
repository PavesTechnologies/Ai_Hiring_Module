from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, Query, Security, UploadFile, status
from fastapi.responses import StreamingResponse
from uuid import UUID
from app.dependencies.campaign_candidate import (
    get_campaign_candidate_service,
)

from app.enums.constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from app.middleware.rbac import TokenUser, get_current_user, require_roles
from app.models.identity import UserRole
from app.models.pipeline import AIEvaluationStatus, AIRecommendation, PipelineStage

from app.schemas.campaign.campaign_candidate_schema import (
    CampaignBoardResponse,
    CampaignCandidateCreateRequest,
    CampaignCandidateResponse,
    CampaignCandidateSummaryResponse,
    CampaignRejectionAnalyticsResponse,
    CandidateAIEvaluationResponse,
    CandidateCompositeResponse,
    CandidateCompositeScoreHistoryResponse,
    CandidateDeterministicResponse,
    CandidateRankingDetailsResponse,
    CandidateRejectionHistoryEntryResponse,
    CandidateScorecardResponse,
    CandidateSemanticResponse,
    CandidateSortField,
    CandidateSummaryResponse,
    CandidateTimelineResponse,
    HrOverrideRequest,
    MovePipelineStageRequest,
    OverrideReportResponse,
    RankedCampaignCandidatesResponse,
    SortOrder,
    UpdateResumeResubmissionResponse,
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
    response_model=APIResponse[RankedCampaignCandidatesResponse],
    status_code=status.HTTP_200_OK,
    summary="Get Ranked Campaign Candidates",
    description=(
        "M10-E03 Phase 1 - retrieve candidates belonging to a campaign, ranked by "
        "composite_score by default (highest first, pending/unscored candidates last), "
        "with filtering, sorting, and pagination. Ranking is always performed by "
        "PostgreSQL, never in application code."
    ),
)
def get_campaign_candidates(
    campaign_id: UUID,
    page: int = Query(default=1, ge=1, description="1-based page number."),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    sort_by: CandidateSortField | None = Query(
        default=None,
        description="Defaults to the composite ranking order (composite_score DESC NULLS LAST, "
        "deterministic_score DESC, created_at ASC, id ASC) when omitted.",
    ),
    sort_order: SortOrder = Query(default="desc"),
    pipeline_stage: PipelineStage | None = Query(default=None),
    composite_score_min: float | None = Query(default=None, ge=0, le=100),
    composite_score_max: float | None = Query(default=None, ge=0, le=100),
    ai_recommendation: AIRecommendation | None = Query(default=None),
    ai_evaluation: AIEvaluationStatus | None = Query(
        default=None, description="Filters by the candidate's ai_evaluation_status.",
    ),
    include_pending: bool = Query(default=True, description="If false, excludes candidates with no composite_score yet."),
    include_rejected: bool = Query(default=True, description="If false, excludes REJECTED-stage candidates."),
    include_fraud: bool = Query(default=True, description="If false, excludes fraud-flagged candidates."),
    hr_override: bool | None = Query(
        default=None, description="If set, filters to only overridden (true) or only non-overridden (false) candidates.",
    ),
    service: CampaignCandidateService = Depends(
        get_campaign_candidate_service,
    ),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
):

    result = service.get_ranked_campaign_candidates(
        campaign_id,
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_order=sort_order,
        pipeline_stage=pipeline_stage,
        composite_score_min=composite_score_min,
        composite_score_max=composite_score_max,
        ai_recommendation=ai_recommendation,
        ai_evaluation_status=ai_evaluation,
        include_pending=include_pending,
        include_rejected=include_rejected,
        include_fraud=include_fraud,
        hr_override=hr_override,
    )

    return APIResponse.ok(
        data=result,
        message="Campaign candidates retrieved successfully.",
    )


@router.get(
    "/campaign/{campaign_id}/board",
    response_model=APIResponse[CampaignBoardResponse],
    status_code=status.HTTP_200_OK,
    summary="Get Pipeline Board",
    description=(
        "Every candidate in the campaign, bucketed by pipeline_stage into "
        "Kanban board columns (Uploaded, Screening, Shortlisted, Hold, "
        "Interview, Selected, Rejected). Reuses the exact same enriched "
        "candidate data the Candidate Listing endpoint returns - no "
        "separate scoring or ranking. HM_REVIEW/FRAUD_REVIEW candidates "
        "aren't part of this board; other_count accounts for them."
    ),
)
def get_campaign_board(
    campaign_id: UUID,
    service: CampaignCandidateService = Depends(
        get_campaign_candidate_service,
    ),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
):
    result = service.get_campaign_board(campaign_id)

    return APIResponse.ok(
        data=result,
        message="Pipeline board retrieved successfully.",
    )


@router.get(
    "/campaign/{campaign_id}/export",
    status_code=status.HTTP_200_OK,
    summary="Export Campaign Ranked Candidate List",
    description=(
        "M10-E03 Phase 3 - exports the campaign's COMPLETE filtered/sorted ranked candidate "
        "list to XLSX (pagination is ignored - every matching candidate, not one page). "
        "HR_ADMIN only. Never includes candidate name/email/phone/resume or any other PII."
    ),
    dependencies=[Security(require_roles(UserRole.HR_ADMIN))],
)
def export_ranked_campaign_candidates(
    campaign_id: UUID,
    sort_by: CandidateSortField | None = Query(
        default=None,
        description="Defaults to the composite ranking order (composite_score DESC NULLS LAST, "
        "deterministic_score DESC, created_at ASC, id ASC) when omitted.",
    ),
    sort_order: SortOrder = Query(default="desc"),
    pipeline_stage: PipelineStage | None = Query(default=None),
    composite_score_min: float | None = Query(default=None, ge=0, le=100),
    composite_score_max: float | None = Query(default=None, ge=0, le=100),
    ai_recommendation: AIRecommendation | None = Query(default=None),
    ai_evaluation: AIEvaluationStatus | None = Query(
        default=None, description="Filters by the candidate's ai_evaluation_status.",
    ),
    include_pending: bool = Query(default=True, description="If false, excludes candidates with no composite_score yet."),
    include_rejected: bool = Query(default=True, description="If false, excludes REJECTED-stage candidates."),
    include_fraud: bool = Query(default=True, description="If false, excludes fraud-flagged candidates."),
    hr_override: bool | None = Query(
        default=None, description="If set, filters to only overridden (true) or only non-overridden (false) candidates.",
    ),
    service: CampaignCandidateService = Depends(
        get_campaign_candidate_service,
    ),
    user: TokenUser = Depends(get_current_user),
) -> StreamingResponse:
    return service.export_ranked_campaign_candidates(
        campaign_id,
        actor_id=user.user_id,
        actor_role=user.roles[0] if user.roles else None,
        sort_by=sort_by,
        sort_order=sort_order,
        pipeline_stage=pipeline_stage,
        composite_score_min=composite_score_min,
        composite_score_max=composite_score_max,
        ai_recommendation=ai_recommendation,
        ai_evaluation_status=ai_evaluation,
        include_pending=include_pending,
        include_rejected=include_rejected,
        include_fraud=include_fraud,
        hr_override=hr_override,
    )


@router.get(
    "/campaign/{campaign_id}/summary",
    response_model=APIResponse[CampaignCandidateSummaryResponse],
    status_code=status.HTTP_200_OK,
    summary="Get Campaign Candidate Ranking Summary",
    description=(
        "M10-E03 Phase 1 - aggregate ranking statistics for a campaign: total/ranked/"
        "pending/rejected/fraud counts, highest/lowest/average composite_score, and "
        "pipeline-stage + AI-recommendation breakdowns. Read-only - never audited."
    ),
)
def get_campaign_candidate_summary(
    campaign_id: UUID,
    service: CampaignCandidateService = Depends(
        get_campaign_candidate_service,
    ),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
):

    summary = service.get_campaign_candidate_summary(campaign_id)

    return APIResponse.ok(
        data=summary,
        message="Campaign candidate summary retrieved successfully.",
    )

@router.get(
    "/campaign/{campaign_id}/export-rejected",
    status_code=status.HTTP_200_OK,
    summary="Export Rejected Candidates",
    description="Exports rejected candidates for a campaign to XLSX. HR_ADMIN only. Never includes PII.",
    dependencies=[Security(require_roles(UserRole.HR_ADMIN))],
)
def export_rejected_candidates(
    campaign_id: UUID,
    service: CampaignCandidateService = Depends(
        get_campaign_candidate_service,
    ),
    user: TokenUser = Depends(get_current_user),
) -> StreamingResponse:
    return service.export_rejected_candidates(
        campaign_id,
        actor_id=user.user_id,
        actor_role=user.roles[0] if user.roles else None,
    )


@router.get(
    "/campaign/{campaign_id}/rejection-analytics",
    response_model=APIResponse[CampaignRejectionAnalyticsResponse],
    status_code=status.HTTP_200_OK,
    summary="Get Campaign Rejection Analytics",
    description=(
        "Deterministic rejection-reason distribution, top missing mandatory skills, and "
        "(once MIN_CANDIDATES_FOR_ANALYTICS is reached) JD calibration recommendations."
    ),
)
def get_campaign_rejection_analytics(
    campaign_id: UUID,
    service: CampaignCandidateService = Depends(
        get_campaign_candidate_service,
    ),
):
    analytics = service.get_campaign_rejection_analytics(campaign_id)

    return APIResponse.ok(
        data=analytics,
        message="Campaign rejection analytics retrieved successfully.",
    )


@router.get(
    "/{campaign_candidate_id}/rejection-history",
    response_model=APIResponse[list[CandidateRejectionHistoryEntryResponse]],
    status_code=status.HTTP_200_OK,
    summary="Get Candidate Rejection History",
    description="Every rejection event for this candidate, newest first. Read-only.",
)
def get_rejection_history(
    campaign_candidate_id: UUID,
    service: CampaignCandidateService = Depends(
        get_campaign_candidate_service,
    ),
):
    history = service.get_rejection_history(campaign_candidate_id)

    return APIResponse.ok(
        data=history,
        message="Rejection history retrieved successfully.",
    )


@router.get(
    "/override-report",
    response_model=APIResponse[OverrideReportResponse],
    status_code=status.HTTP_200_OK,
    summary="Get HR Override Report",
    description=(
        "HR_ADMIN override events, optionally filtered by campaign and date range, plus a "
        "weekly trend (last 8 weeks) and per-campaign override-rate alerting. HR_ADMIN only."
    ),
    dependencies=[Security(require_roles(UserRole.HR_ADMIN))],
)
def get_override_report(
    campaign_id: UUID | None = Query(default=None),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    service: CampaignCandidateService = Depends(
        get_campaign_candidate_service,
    ),
):
    report = service.get_override_report(
        campaign_id=campaign_id, date_from=date_from, date_to=date_to,
    )

    return APIResponse.ok(
        data=report,
        message="Override report retrieved successfully.",
    )


@router.get(
    "/override-report/export",
    status_code=status.HTTP_200_OK,
    summary="Export HR Override Report",
    description="Exports the HR override report to XLSX. HR_ADMIN only. Never includes candidate PII.",
    dependencies=[Security(require_roles(UserRole.HR_ADMIN))],
)
def export_override_report(
    campaign_id: UUID | None = Query(default=None),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    service: CampaignCandidateService = Depends(
        get_campaign_candidate_service,
    ),
    user: TokenUser = Depends(get_current_user),
) -> StreamingResponse:
    return service.export_override_report(
        campaign_id=campaign_id,
        date_from=date_from,
        date_to=date_to,
        actor_id=user.user_id,
        actor_role=user.roles[0] if user.roles else None,
    )


@router.post(
    "/{campaign_candidate_id}/override",
    response_model=APIResponse[CandidateScorecardResponse],
    status_code=status.HTTP_200_OK,
    summary="Apply HR Override",
    description=(
        "HR_ADMIN override of a deterministic rejection - re-enters the candidate into "
        "SCREENING. HR_ADMIN only."
    ),
    dependencies=[Security(require_roles(UserRole.HR_ADMIN))],
)
def apply_hr_override(
    campaign_candidate_id: UUID,
    request: HrOverrideRequest,
    service: CampaignCandidateService = Depends(
        get_campaign_candidate_service,
    ),
    user: TokenUser = Depends(get_current_user),
):
    scorecard = service.apply_hr_override(
        campaign_candidate_id,
        override_reason=request.override_reason,
        actor_id=user.user_id,
        actor_role=user.roles[0] if user.roles else None,
    )

    return APIResponse.ok(
        data=scorecard,
        message="HR override applied successfully.",
    )


@router.post(
    "/{campaign_candidate_id}/stage",
    response_model=APIResponse[CampaignCandidateResponse],
    status_code=status.HTTP_200_OK,
    summary="Move Pipeline Stage (Pipeline Board drag-and-drop)",
    description=(
        "Moves one candidate to an arbitrary target pipeline_stage - the "
        "Pipeline Board's drag-and-drop action. Backed entirely by "
        "PipelineTransitionService: whether the move is allowed at all, "
        "which roles may perform it, and whether a reason is required all "
        "depend on the allowed_transitions row for this exact from/to "
        "pair, not on role alone."
    ),
)
def move_campaign_candidate_stage(
    campaign_candidate_id: UUID,
    request: MovePipelineStageRequest,
    service: CampaignCandidateService = Depends(
        get_campaign_candidate_service,
    ),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
):
    result = service.move_pipeline_stage(
        campaign_candidate_id,
        to_stage=request.to_stage,
        actor_id=user.user_id,
        actor_role=user.roles[0] if user.roles else None,
        reason=request.reason,
    )

    return APIResponse.ok(
        data=result,
        message="Candidate moved successfully.",
    )


@router.post(
    "/{campaign_candidate_id}/update-resume",
    response_model=APIResponse[UpdateResumeResubmissionResponse],
    status_code=status.HTTP_200_OK,
    summary="Update Resume (Resubmission)",
    description=(
        "Epic 3 (M05-E03) Phase C5 - resolves an existing "
        "campaign+candidate pairing by uploading a new resume version, "
        "resetting evaluation state, and re-triggering the pipeline. "
        "Whether this is allowed at all, and whether it requires HR_ADMIN, "
        "depends on the candidate's current pipeline_stage (validated by "
        "PipelineTransitionService against allowed_transitions, not by "
        "role alone) - RECRUITER can trigger it before SHORTLISTED; "
        "HR_ADMIN only afterward."
    ),
)
def update_resume_for_resubmission(
    campaign_candidate_id: UUID,
    reason: str | None = Form(default=None),
    file: UploadFile = File(...),
    service: CampaignCandidateService = Depends(
        get_campaign_candidate_service,
    ),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    file_bytes = file.file.read()
    filename = file.filename or "resume"

    result = service.update_resume_for_resubmission(
        campaign_candidate_id,
        file_bytes=file_bytes,
        filename=filename,
        actor_id=user.user_id,
        actor_role=user.roles[0] if user.roles else None,
        reason=reason,
        content_type=file.content_type,
    )

    return APIResponse.ok(
        data=result,
        message="Resume updated and queued for re-evaluation.",
    )


@router.get(
    "/rejection-analytics/export",
    status_code=status.HTTP_200_OK,
    summary="Export Platform-wide Deterministic Rejection Summary",
    description=(
        "Exports a 3-sheet XLSX (Campaign Summary, Skill Gap Analysis, Override Log) across "
        "every campaign. HR_ADMIN only. Never includes candidate PII."
    ),
    dependencies=[Security(require_roles(UserRole.HR_ADMIN))],
)
def export_deterministic_rejection_summary(
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    service: CampaignCandidateService = Depends(
        get_campaign_candidate_service,
    ),
    user: TokenUser = Depends(get_current_user),
) -> StreamingResponse:
    return service.export_deterministic_rejection_summary(
        date_from=date_from,
        date_to=date_to,
        actor_id=user.user_id,
        actor_role=user.roles[0] if user.roles else None,
    )


@router.get(
    "/{campaign_candidate_id}/summary",
    response_model=APIResponse[CandidateSummaryResponse],
    status_code=status.HTTP_200_OK,
    summary="Get Candidate Summary (Summary tab)",
    description=(
        "Summary-tab-only view: header, candidate info, overall scores, AI summary "
        "(if available). Excludes deterministic breakdown, resume, semantic, AI evaluation, "
        "and final status data - see the other tab endpoints for those."
    ),
)
def get_candidate_summary(
    campaign_candidate_id: UUID,
    service: CampaignCandidateService = Depends(
        get_campaign_candidate_service,
    ),
):
    summary = service.get_candidate_summary(campaign_candidate_id)

    return APIResponse.ok(
        data=summary,
        message="Candidate summary retrieved successfully.",
    )


@router.get(
    "/{campaign_candidate_id}/deterministic",
    response_model=APIResponse[CandidateDeterministicResponse],
    status_code=status.HTTP_200_OK,
    summary="Get Candidate Deterministic Score Breakdown (Deterministic tab)",
    description=(
        "Deterministic-tab-only view: deterministic_score + deterministic_score_breakdown. "
        "Excludes summary, resume, semantic, AI evaluation, and final status data."
    ),
)
def get_candidate_deterministic(
    campaign_candidate_id: UUID,
    service: CampaignCandidateService = Depends(
        get_campaign_candidate_service,
    ),
):
    deterministic = service.get_candidate_deterministic(campaign_candidate_id)

    return APIResponse.ok(
        data=deterministic,
        message="Candidate deterministic score retrieved successfully.",
    )


@router.get(
    "/{campaign_candidate_id}/semantic",
    response_model=APIResponse[CandidateSemanticResponse],
    status_code=status.HTTP_200_OK,
    summary="Get Candidate Semantic Score Breakdown (Semantic tab)",
    description=(
        "Semantic-tab-only view: semantic_score + semantic_score_breakdown. "
        "Excludes summary, resume, deterministic, AI evaluation, and final status data."
    ),
)
def get_candidate_semantic(
    campaign_candidate_id: UUID,
    service: CampaignCandidateService = Depends(
        get_campaign_candidate_service,
    ),
):
    semantic = service.get_candidate_semantic(campaign_candidate_id)

    return APIResponse.ok(
        data=semantic,
        message="Candidate semantic score retrieved successfully.",
    )


@router.get(
    "/{campaign_candidate_id}/ai-evaluation",
    response_model=APIResponse[CandidateAIEvaluationResponse],
    status_code=status.HTTP_200_OK,
    summary="Get Candidate AI Evaluation Result (AI Evaluation tab)",
    description=(
        "AI-Evaluation-tab-only view: effective_ai_score, ai_confidence, ai_recommendation, "
        "ai_strengths, ai_weaknesses, ai_evaluation_status, and the complete validated AI "
        "response JSON exactly as returned by the LLM. Excludes summary, resume, "
        "deterministic, semantic, and final status data."
    ),
)
def get_candidate_ai_evaluation(
    campaign_candidate_id: UUID,
    service: CampaignCandidateService = Depends(
        get_campaign_candidate_service,
    ),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
):
    ai_evaluation = service.get_candidate_ai_evaluation(campaign_candidate_id)

    return APIResponse.ok(
        data=ai_evaluation,
        message="Candidate AI evaluation result retrieved successfully.",
    )


@router.get(
    "/{campaign_candidate_id}/composite",
    response_model=APIResponse[CandidateCompositeResponse],
    status_code=status.HTTP_200_OK,
    summary="Get Candidate Composite Score Details (Composite tab)",
    description=(
        "Composite-tab-only view: composite_score, component scores, current campaign "
        "weights, formula version, ranking status, and computed timestamp. Read-only - "
        "never recalculates composite_score."
    ),
)
def get_candidate_composite(
    campaign_candidate_id: UUID,
    service: CampaignCandidateService = Depends(
        get_campaign_candidate_service,
    ),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
):
    composite = service.get_candidate_composite(campaign_candidate_id)

    return APIResponse.ok(
        data=composite,
        message="Candidate composite score details retrieved successfully.",
    )


# Future tabs (not implemented yet, per this story's explicit scope):
# GET /{campaign_candidate_id}/resume, GET /{campaign_candidate_id}/final-status.
# Each would follow the exact same pattern as summary/deterministic/semantic/
# ai-evaluation above: its own small response schema + its own
# get_candidate_<tab>() service method, reusing existing mapper helpers
# rather than recomputing.


@router.get(
    "/{campaign_candidate_id}",
    response_model=APIResponse[CandidateScorecardResponse],
    status_code=status.HTTP_200_OK,
    summary="Get Candidate Scorecard",
    description="Single-candidate detail view, including the rejection banner when applicable.",
)
def get_campaign_candidate_scorecard(
    campaign_candidate_id: UUID,
    service: CampaignCandidateService = Depends(
        get_campaign_candidate_service,
    ),
):
    scorecard = service.get_campaign_candidate_scorecard(campaign_candidate_id)

    return APIResponse.ok(
        data=scorecard,
        message="Candidate scorecard retrieved successfully.",
    )


@router.get(
    "/{campaign_candidate_id}/timeline",
    response_model=APIResponse[CandidateTimelineResponse],
    status_code=status.HTTP_200_OK,
    summary="Get Candidate Stage Timeline",
    description=(
        "M10-E03 Phase 2 - the complete pipeline-stage transition history for one candidate "
        "(current stage plus every transition, oldest first). Read-only - never audited."
    ),
)
def get_candidate_timeline(
    campaign_candidate_id: UUID,
    service: CampaignCandidateService = Depends(
        get_campaign_candidate_service,
    ),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
):
    timeline = service.get_candidate_timeline(campaign_candidate_id)

    return APIResponse.ok(
        data=timeline,
        message="Candidate timeline retrieved successfully.",
    )


@router.get(
    "/{campaign_candidate_id}/composite-history",
    response_model=APIResponse[CandidateCompositeScoreHistoryResponse],
    status_code=status.HTTP_200_OK,
    summary="Get Candidate Composite Score History",
    description=(
        "M10-E03 Phase 2 - the complete, immutable composite-score calculation history for one "
        "candidate (Epic 1's candidate_composite_score_history), most recent first, returned "
        "exactly as stored. Read-only - never recalculates, never audited."
    ),
)
def get_candidate_composite_history(
    campaign_candidate_id: UUID,
    service: CampaignCandidateService = Depends(
        get_campaign_candidate_service,
    ),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
):
    history = service.get_candidate_composite_history(campaign_candidate_id)

    return APIResponse.ok(
        data=history,
        message="Candidate composite score history retrieved successfully.",
    )


@router.get(
    "/{campaign_candidate_id}/ranking-details",
    response_model=APIResponse[CandidateRankingDetailsResponse],
    status_code=status.HTTP_200_OK,
    summary="Get Candidate Ranking Details",
    description=(
        "M10-E03 Phase 2 - explains why this candidate currently has its ranking: current scores, "
        "current campaign weights, formula version, ranking status, and HR override state. "
        "Read-only - never recalculates anything."
    ),
)
def get_candidate_ranking_details(
    campaign_candidate_id: UUID,
    service: CampaignCandidateService = Depends(
        get_campaign_candidate_service,
    ),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
):
    details = service.get_candidate_ranking_details(campaign_candidate_id)

    return APIResponse.ok(
        data=details,
        message="Candidate ranking details retrieved successfully.",
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
