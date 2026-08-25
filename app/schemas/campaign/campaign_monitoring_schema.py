from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


# ── Stalled candidates ─────────────────────────────────────────────────

class StalledCandidateItem(BaseModel):
    campaign_candidate_id: UUID
    candidate_name: str                  # decrypted full name - added for the frontend's Stalled tab
    pipeline_stage: str
    days_stalled: float
    last_updated_at: datetime
    stall_reason: str                    # AI_EVALUATION_FAILED / SCREENING_OVERDUE / HM_REVIEW_OVERDUE / INTERVIEW_NOT_SCHEDULED
    last_action_by: str | None
    has_dead_letter_tasks: bool          # Re-Process is only meaningful when True


class StalledCandidatesResponse(BaseModel):
    items: list[StalledCandidateItem]
    total: int
    sla_config: dict[str, float]         # the thresholds the flags were computed against


class StageOverrideRequest(BaseModel):
    """HR_ADMIN manually advances the candidate. Reason is mandatory."""
    reason: str = Field(..., min_length=1, max_length=1000)
    target_stage: Optional[str] = None   # defaults to the natural next stage


class FlagReviewRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=1000)


class EscalateStallRequest(BaseModel):
    note: Optional[str] = Field(default=None, max_length=1000)


class StalledActionResponse(BaseModel):
    campaign_candidate_id: UUID
    action: str                          # REPROCESSED / ESCALATED / STAGE_OVERRIDDEN / FLAGGED_FOR_REVIEW
    detail: str
    from_stage: str | None = None
    to_stage: str | None = None
    replayed_count: int | None = None


# ── Rejection analytics ────────────────────────────────────────────────

class RejectionReasonItem(BaseModel):
    reason: str
    count: int
    percentage: float


class MissingSkillItem(BaseModel):
    canonical_name: str
    count: int
    percentage_of_deterministic: float


class RejectionRecommendation(BaseModel):
    condition: str                       # e.g. DETERMINISTIC_REJECTION_RATE_HIGH
    layer: str
    rate_pct: float
    threshold_pct: float
    recommendation: str
    action: str                          # REVIEW_JD_SKILLS / ADJUST_THRESHOLD / REVIEW_PROMPT


class RejectionAnalyticsResponse(BaseModel):
    total_candidates: int
    total_rejections: int
    layer_breakdown: dict[str, int]      # DETERMINISTIC/SEMANTIC/AI/MANUAL/FRAUD → count
    top_reasons: list[RejectionReasonItem]
    top_missing_skill: MissingSkillItem | None
    missing_skills: list[MissingSkillItem]
    analytics_ready: bool                # false until MIN_CANDIDATES_FOR_ANALYTICS reached
    min_candidates_required: int
    recommendations: list[RejectionRecommendation]
