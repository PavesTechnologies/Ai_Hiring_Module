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


class SkillSuggestionResponse(BaseModel):
    canonical_skill_id: UUID
    canonical_name: str
    category: str | None
    # Distinct candidates in this campaign holding the skill on the resume
    # they submitted here — not a global occurrence count.
    candidate_count: int


class SkillFilterResponse(BaseModel):
    campaign_candidate_ids: list[UUID]
    # Reserved for a future confidence tiering (e.g. exact vs alias match);
    # every hit here is an exact canonical-skill AND match, so always empty.
    match_tiers: dict[str, int] = {}
    result_count: int


class CandidateFilterResponse(BaseModel):
    campaign_candidate_ids: list[UUID]


class UploaderResponse(BaseModel):
    user_id: str
    full_name: str
    upload_count: int


class StageTimingResponse(BaseModel):
    stage: str
    avg_days: float
    max_days: float
    # No SLA concept exists in the schema yet — always null/false rather
    # than a fabricated threshold. Kept on the response so the frontend's
    # existing conditional rendering has a stable shape to check against.
    sla_days: float | None = None
    breaches_sla: bool = False
