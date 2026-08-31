from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class StatTileResponse(BaseModel):
    value: float
    unit: str | None = None
    delta: float | None = None
    delta_label: str | None = None
    # True for tiles built from a proxy metric rather than a dedicated
    # domain concept (e.g. "offers" read off the SELECTED pipeline
    # decision — there is no distinct offer/acceptance stage yet).
    is_estimate: bool = False


class DashboardStatsResponse(BaseModel):
    open_campaigns: StatTileResponse
    candidates_in_pipeline: StatTileResponse
    avg_time_to_hire_days: StatTileResponse
    offers_this_quarter: StatTileResponse


class FunnelStageCount(BaseModel):
    stage: str
    label: str
    count: int


class HiringFunnelResponse(BaseModel):
    range_days: int | None
    total_candidates: int
    stages: list[FunnelStageCount]


class TopCandidateResponse(BaseModel):
    campaign_candidate_id: UUID
    candidate_id: UUID
    campaign_id: UUID
    campaign_name: str
    candidate_name: str | None
    current_designation: str | None
    composite_score: float


class TopCandidatesResponse(BaseModel):
    candidates: list[TopCandidateResponse]


class NotificationItemResponse(BaseModel):
    id: str
    event_type: str
    message: str
    actor_name: str
    campaign_id: UUID | None
    created_at: datetime


class NotificationsFeedResponse(BaseModel):
    items: list[NotificationItemResponse]


class CircuitBreakerHealthResponse(BaseModel):
    """One external dependency's current breaker state (M11-E01-S01-T02)."""

    service_name: str
    state: str
    failure_count: int
    opened_at: datetime | None = None
    retry_after: datetime | None = None


class HrAdminDashboardSummaryResponse(BaseModel):
    """Platform-wide activity summary. HR_ADMIN only."""

    active_campaigns: int
    candidates_last_7_days: int
    shortlisted_candidates: int
    hm_review_pending: int
    campaigns_with_stall_warnings: int
    ai_evaluation_failures: int
    pending_unknown_skills: int
    platform_health: list[CircuitBreakerHealthResponse]
    # users.last_login_at. Nothing writes it today because authentication
    # happens in the external UMS, so this is null until UMS populates it -
    # the UI hides the field rather than rendering a placeholder.
    last_login_at: datetime | None = None
    generated_at: datetime


class RecruiterDashboardSummaryResponse(BaseModel):
    """Own-activity summary. RECRUITER only - never platform-wide metrics."""

    campaigns_uploaded_to: int
    campaigns_created: int
    resumes_last_7_days: int
    shortlisted_from_my_uploads: int
    failed_bulk_jobs: int
    last_login_at: datetime | None = None
    generated_at: datetime


class NavBadgeCountsResponse(BaseModel):
    """
    Cross-campaign counts for the nav badges (M11-E01-S03-T03). A zero is
    returned as-is; hiding zero badges is the UI's decision, not the API's.
    """

    pending_reviews: int
    fraud_review: int
    ai_failures: int
    generated_at: datetime


class StageTimingResponse(BaseModel):
    """Time candidates have spent in their current stage (M11-E01-S04-T02)."""

    stage: str
    candidate_count: int
    avg_days: float
    max_days: float
    # SLA for this stage expressed in days, and whether max_days breaches it
    sla_days: float | None = None
    breaches_sla: bool = False


class DashboardCampaignCardResponse(BaseModel):
    """
    One campaign card (M11-E01-S02-T01 + S03-T01).

    Deliberately lighter than CampaignResponse: every count here is produced by
    a single grouped query across all cards, rather than the per-campaign
    follow-up queries the existing campaign list endpoints issue.
    """

    id: UUID
    name: str
    status: str
    jd_id: UUID | None = None
    jd_title: str | None = None
    jd_version: int | None = None
    hiring_manager: str | None = None
    max_candidates: int | None = None
    deadline: datetime | None = None
    created_at: datetime

    candidate_count: int
    shortlisted_count: int
    selected_count: int
    hm_review_count: int
    ai_failure_count: int
    stalled_count: int

    # health indicators, precomputed so the UI never re-derives thresholds
    approaching_cap: bool
    deadline_soon: bool
    is_overdue: bool
