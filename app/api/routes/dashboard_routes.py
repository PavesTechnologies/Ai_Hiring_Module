from fastapi import APIRouter, Depends, Query, Security, status

from app.dependencies.dashboard import get_dashboard_service
from app.middleware.rbac import TokenUser, require_roles
from app.models.identity import UserRole
from app.schemas.dashboard.dashboard_response import (
    DashboardStatsResponse,
    HiringFunnelResponse,
    NotificationsFeedResponse,
    TopCandidatesResponse,
)
from app.schemas.response import APIResponse
from app.services.dashboard.dashboard_service import DashboardService

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


@router.get("/stats",
    response_model=APIResponse[DashboardStatsResponse],
    status_code=status.HTTP_200_OK,
    summary="Get dashboard stat tiles",
    description=(
        "Open campaigns, candidates in pipeline, average time to hire, and offers "
        "this quarter, each with a period-over-period delta. avg_time_to_hire_days "
        "and offers_this_quarter come back with is_estimate=true: there is no "
        "dedicated offer/acceptance stage or requisition-open date in the schema "
        "yet, so 'offers' reads the SELECTED pipeline decision and 'time to hire' "
        "is measured from campaign_candidates.created_at to that decision."
    ),
)
def get_dashboard_stats(
    service: DashboardService = Depends(get_dashboard_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
):
    return APIResponse.ok(data=service.get_stats(), message="Dashboard stats retrieved successfully.")


@router.get("/hiring-funnel",
    response_model=APIResponse[HiringFunnelResponse],
    status_code=status.HTTP_200_OK,
    summary="Get platform-wide hiring funnel",
    description=(
        "Candidate counts across all ACTIVE campaigns for Uploaded, Parsing, "
        "Screening, Shortlisted, Interview, and Selected, optionally scoped to the "
        "last N days. Uploaded/Parsing come from resumes.parse_status; the "
        "remaining stages are the current campaign_candidates.pipeline_stage "
        "snapshot (not a historical cumulative count) — the same convention "
        "already used by GET /campaigns/{id}/pipeline-summary."
    ),
)
def get_hiring_funnel(
    days: int | None = Query(default=30, ge=1, le=365, description="Restrict to records created in the last N days. Omit for all-time."),
    service: DashboardService = Depends(get_dashboard_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
):
    return APIResponse.ok(data=service.get_hiring_funnel(days), message="Hiring funnel retrieved successfully.")


@router.get("/top-candidates",
    response_model=APIResponse[TopCandidatesResponse],
    status_code=status.HTTP_200_OK,
    summary="Get top-ranked candidates across all active campaigns",
    description="Highest composite_score candidates across every ACTIVE campaign, highest first.",
)
def get_top_candidates(
    limit: int = Query(default=5, ge=1, le=50),
    service: DashboardService = Depends(get_dashboard_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
):
    return APIResponse.ok(data=service.get_top_candidates(limit), message="Top candidates retrieved successfully.")


@router.get("/notifications",
    response_model=APIResponse[NotificationsFeedResponse],
    status_code=status.HTTP_200_OK,
    summary="Get recent platform activity feed",
    description=(
        "Best-effort 'Tasks & notifications' feed built from the existing audit "
        "log (app.models.compliance.AuditLog) — there is no dedicated "
        "notifications table yet, so this only surfaces events that are already "
        "audited (campaign/candidate/JD/resume lifecycle events). It does not "
        "cover duplicate-resume flags, JD approval workflow, hiring-manager "
        "feedback, or interview scheduling, since none of those exist as "
        "persisted events in the schema yet."
    ),
)
def get_dashboard_notifications(
    limit: int = Query(default=10, ge=1, le=100),
    service: DashboardService = Depends(get_dashboard_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER)),
):
    return APIResponse.ok(data=service.get_notifications(limit), message="Notifications retrieved successfully.")
