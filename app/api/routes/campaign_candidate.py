from datetime import datetime

from fastapi import APIRouter, Depends, Query, Security, status
from fastapi.responses import StreamingResponse
from uuid import UUID
from app.dependencies.campaign_candidate import (
    get_campaign_candidate_service,
)

from app.middleware.rbac import TokenUser, get_current_user, require_roles
from app.models.identity import UserRole

from app.schemas.campaign.campaign_candidate_schema import (
    CampaignCandidateCreateRequest,
    CampaignCandidateResponse,
    CampaignRejectionAnalyticsResponse,
    CandidateRejectionHistoryEntryResponse,
    CandidateScorecardResponse,
    HrOverrideRequest,
    OverrideReportResponse,
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
    description="Every candidate_rejections record for this candidate, newest first. Read-only.",
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