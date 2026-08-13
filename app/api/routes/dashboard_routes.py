from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Security, status

from app.dependencies.dashboard import get_dashboard_service, get_candidate_search_service
from app.middleware.rbac import TokenUser, require_roles
from app.models.campaigns import CampaignStatus
from app.models.identity import UserRole
from app.schemas.dashboard.dashboard_response import (
    DashboardCampaignCardResponse,
    HrAdminDashboardSummaryResponse,
    NavBadgeCountsResponse,
    RecruiterDashboardSummaryResponse,
    StageTimingResponse,
)
from app.exceptions.campaign_exceptions import CampaignException
from app.models.pipeline import PipelineStage
from app.schemas.dashboard.candidate_search_schema import (
    CampaignUploaderResponse,
    CandidateFilterResultResponse,
    SkillFilterResultResponse,
    SkillSuggestionResponse,
)
from app.services.dashboard.candidate_search_service import CandidateSearchService
from app.schemas.response import APIResponse
from app.services.dashboard.dashboard_service import DashboardService

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


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
    hiring_manager_id: str | None = Query(default=None),
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
        hiring_manager_id=hiring_manager_id,
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
    experience_min: float | None = Query(default=None, ge=0),
    experience_max: float | None = Query(default=None, ge=0),
    include_unknown_experience: bool = Query(
        default=True,
        description="Keep candidates whose experience could not be parsed (unknown ≠ zero).",
    ),
    degree_levels: list[str] | None = Query(
        default=None, description="PHD | MASTER | BACHELOR | DIPLOMA | ASSOCIATE | CERTIFICATION",
    ),
    uploaded_by: str | None = Query(default=None),
    uploaded_from: datetime | None = Query(default=None),
    uploaded_to: datetime | None = Query(default=None),
    upload_type: str | None = Query(default=None, description="individual | bulk"),
    service: CandidateSearchService = Depends(get_candidate_search_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN, UserRole.RECRUITER)),
):
    ids = service.filter_candidates(
        campaign_id,
        experience_min=experience_min,
        experience_max=experience_max,
        include_unknown_experience=include_unknown_experience,
        degree_levels=degree_levels,
        uploaded_by=uploaded_by,
        uploaded_from=uploaded_from,
        uploaded_to=uploaded_to,
        upload_type=upload_type,
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
