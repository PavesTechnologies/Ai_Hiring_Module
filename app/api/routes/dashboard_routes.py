from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Security, status

from app.dependencies.dashboard import get_candidate_search_service, get_dashboard_service
from app.middleware.rbac import TokenUser, require_roles
from app.models.campaigns import CampaignStatus
from app.models.identity import UserRole
from app.schemas.dashboard.candidate_search_schema import (
    CampaignUploaderResponse,
    CandidateFilterResultResponse,
    SkillFilterResultResponse,
    SkillSuggestionResponse,
)
from app.schemas.dashboard.dashboard_response import (
    DashboardCampaignCardResponse,
    DashboardStatsResponse,
    HiringFunnelResponse,
    HrAdminDashboardSummaryResponse,
    NavBadgeCountsResponse,
    NotificationsFeedResponse,
    RecruiterDashboardSummaryResponse,
    StageTimingResponse,
    TopCandidatesResponse,
)
from app.schemas.response import APIResponse
from app.services.dashboard.candidate_search_service import CandidateSearchService
from app.services.dashboard.dashboard_service import DashboardService

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


# Native HTML number/date inputs report "" (not an omitted param) when left
# blank, and a float|None / datetime|None Query type 422s on "" — so these
# fields are accepted as raw strings and parsed here, treating blank as
# "not provided" instead of a validation error.
def _parse_optional_float(value: str | None, field_name: str) -> float | None:
    if value is None or value.strip() == "":
        return None
    try:
        parsed = float(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{field_name} must be a number")
    if parsed < 0:
        raise HTTPException(status_code=422, detail=f"{field_name} must be >= 0")
    return parsed


def _parse_optional_datetime(value: str | None, field_name: str) -> datetime | None:
    if value is None or value.strip() == "":
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{field_name} must be a valid date/datetime")


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


@router.get(
    "/hr-admin/summary",
    response_model=APIResponse[HrAdminDashboardSummaryResponse],
    status_code=status.HTTP_200_OK,
    summary="Platform-wide activity summary (HR_ADMIN)",
    description="Org-wide hiring activity plus external-dependency health.",
)
def get_hr_admin_summary(
    service: DashboardService = Depends(get_dashboard_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    return APIResponse.ok(
        data=service.get_hr_admin_summary(user.user_id),
        message="Dashboard summary retrieved successfully",
    )


@router.get(
    "/recruiter/summary",
    response_model=APIResponse[RecruiterDashboardSummaryResponse],
    status_code=status.HTTP_200_OK,
    summary="Own-activity summary (RECRUITER)",
    description="Scoped to the caller's own uploads and campaigns.",
)
def get_recruiter_summary(
    service: DashboardService = Depends(get_dashboard_service),
    user: TokenUser = Security(require_roles(UserRole.RECRUITER)),
):
    return APIResponse.ok(
        data=service.get_recruiter_summary(user.user_id),
        message="Dashboard summary retrieved successfully",
    )


@router.get(
    "/campaigns",
    response_model=APIResponse[list[DashboardCampaignCardResponse]],
    status_code=status.HTTP_200_OK,
    summary="Campaign summary cards",
    description=(
        "T01 — campaign cards with per-stage counts and health "
        "indicators. HR_ADMIN sees every campaign; RECRUITER is scoped to campaigns they "
        "uploaded to or created."
    ),
)
def get_dashboard_campaigns(
    show_closed: bool = Query(default=False),
    limit: int = Query(default=12, ge=1, le=50),
    search: str | None = Query(default=None, description="Matches campaign name or JD title."),
    status_filter: CampaignStatus | None = Query(default=None, alias="status"),
    service: DashboardService = Depends(get_dashboard_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    # scoping is decided here, never by the client
    is_hr_admin = UserRole.HR_ADMIN.value in (user.roles or [])
    cards = service.get_campaign_cards(
        recruiter_id=None if is_hr_admin else user.user_id,
        show_closed=show_closed,
        limit=limit,
        search=search,
        status=status_filter,
    )
    return APIResponse.ok(data=cards, message="Campaign cards retrieved successfully")


@router.get(
    "/badges",
    response_model=APIResponse[NavBadgeCountsResponse],
    status_code=status.HTTP_200_OK,
    summary="Live nav badge counts",
    description=(
        "Cross-campaign pending reviews, fraud-review queue and AI "
        "failures. RECRUITER counts are scoped to their accessible campaigns."
    ),
)
def get_nav_badges(
    service: DashboardService = Depends(get_dashboard_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    is_hr_admin = UserRole.HR_ADMIN.value in (user.roles or [])
    return APIResponse.ok(
        data=service.get_nav_badges(None if is_hr_admin else user.user_id),
        message="Badge counts retrieved successfully",
    )


@router.get(
    "/campaigns/{campaign_id}/skill-suggestions",
    response_model=APIResponse[list[SkillSuggestionResponse]],
    status_code=status.HTTP_200_OK,
    summary="Skill autocomplete within a campaign",
    description=(
        "Canonical skills matching the query by name or alias, "
        "restricted to skills candidates in this campaign actually hold."
    ),
)
def suggest_skills(
    campaign_id: UUID,
    q: str = Query(..., min_length=1, description="Partial skill name or alias."),
    limit: int = Query(default=10, ge=1, le=25),
    service: CandidateSearchService = Depends(get_candidate_search_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    return APIResponse.ok(
        data=service.suggest_skills(campaign_id, q, limit),
        message="Skill suggestions retrieved successfully",
    )


@router.get(
    "/campaigns/{campaign_id}/skill-filter",
    response_model=APIResponse[SkillFilterResultResponse],
    status_code=status.HTTP_200_OK,
    summary="Candidates holding ALL of the given skills",
    description=(
        "AND logic: a candidate must hold every requested skill. "
        "Returns campaign_candidate ids plus how each skill matched, and logs the "
        "search to search_queries (T03), including zero-result searches."
    ),
)
def filter_candidates_by_skill(
    campaign_id: UUID,
    skill_ids: list[UUID] = Query(..., description="Repeat for each skill; AND-combined."),
    q: str = Query(default="", description="Raw text typed, for search analytics."),
    service: CandidateSearchService = Depends(get_candidate_search_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    ids, tiers = service.resolve_skill_filter(
        campaign_id=campaign_id,
        skill_ids=skill_ids,
        user_id=user.user_id,
        query_text=q,
    )
    return APIResponse.ok(
        data=SkillFilterResultResponse(
            campaign_candidate_ids=ids,
            match_tiers=tiers,
            result_count=len(ids),
        ),
        message="Skill filter applied successfully",
    )


@router.get(
    "/campaigns/{campaign_id}/candidate-filter",
    response_model=APIResponse[CandidateFilterResultResponse],
    status_code=status.HTTP_200_OK,
    summary="Filter candidates by resume-derived criteria",
    description=(
        "Experience years, education level and upload source. "
        "These live in resumes.parsed_json rather than on campaign_candidates, so "
        "they are resolved here and intersected with the other active filters. "
        "NOTE: the parser emits education as free-text `degree` with no normalised "
        "degree_level, so the education filter pattern-matches that text."
    ),
)
def filter_candidates(
    campaign_id: UUID,
    experience_min: str | None = Query(default=None, description="Minimum years of experience. Blank means not set."),
    experience_max: str | None = Query(default=None, description="Maximum years of experience. Blank means not set."),
    include_unknown_experience: bool = Query(
        default=True,
        description="Keep candidates whose experience could not be parsed (unknown ≠ zero).",
    ),
    degree_levels: list[str] | None = Query(
        default=None, description="PHD | MASTER | BACHELOR | DIPLOMA | ASSOCIATE | CERTIFICATION",
    ),
    uploaded_by: str | None = Query(default=None),
    uploaded_from: str | None = Query(default=None, description="Blank means not set."),
    uploaded_to: str | None = Query(default=None, description="Blank means not set."),
    upload_type: str | None = Query(default=None, description="individual | bulk"),
    service: CandidateSearchService = Depends(get_candidate_search_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    ids = service.filter_candidates(
        campaign_id,
        experience_min=_parse_optional_float(experience_min, "experience_min"),
        experience_max=_parse_optional_float(experience_max, "experience_max"),
        include_unknown_experience=include_unknown_experience,
        degree_levels=degree_levels,
        uploaded_by=uploaded_by or None,
        uploaded_from=_parse_optional_datetime(uploaded_from, "uploaded_from"),
        uploaded_to=_parse_optional_datetime(uploaded_to, "uploaded_to"),
        upload_type=upload_type or None,
    )
    return APIResponse.ok(
        data=CandidateFilterResultResponse(
            campaign_candidate_ids=ids or [],
            result_count=len(ids) if ids is not None else 0,
        ),
        message="Candidate filter applied successfully",
    )


@router.get(
    "/campaigns/{campaign_id}/uploaders",
    response_model=APIResponse[list[CampaignUploaderResponse]],
    status_code=status.HTTP_200_OK,
    summary="Distinct uploaders for a campaign",
    description="Populates the 'Uploaded By' filter dropdown.",
)
def get_campaign_uploaders(
    campaign_id: UUID,
    service: CandidateSearchService = Depends(get_candidate_search_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    rows = service.get_campaign_uploaders(campaign_id)
    return APIResponse.ok(
        data=[
            CampaignUploaderResponse(
                user_id=str(r.id), full_name=r.full_name, upload_count=r.upload_count
            )
            for r in rows
        ],
        message="Uploaders retrieved successfully",
    )


@router.get(
    "/campaigns/{campaign_id}/stage-timing",
    response_model=APIResponse[list[StageTimingResponse]],
    status_code=status.HTTP_200_OK,
    summary="Stage dwell times (HR_ADMIN)",
    description=(
        "Average and maximum days candidates have spent in their "
        "current stage, with the configured SLA and breach flag per stage."
    ),
)
def get_stage_timing(
    campaign_id: UUID,
    service: DashboardService = Depends(get_dashboard_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    return APIResponse.ok(
        data=service.get_stage_timing(campaign_id),
        message="Stage timing retrieved successfully",
    )
