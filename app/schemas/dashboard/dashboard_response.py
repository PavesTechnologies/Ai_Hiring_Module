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
