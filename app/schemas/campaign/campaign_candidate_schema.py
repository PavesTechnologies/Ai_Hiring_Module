from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from datetime import date, datetime

from app.models.pipeline import PipelineStage, RejectionLayer

class CampaignCandidateCreateRequest(BaseModel):
    campaign_id: UUID
    candidate_id: UUID
    resume_id: UUID

    model_config = ConfigDict(
        from_attributes=True,
    )

class CampaignCandidateResponse(BaseModel):
    id: UUID
    campaign_id: UUID
    candidate_id: UUID
    # Same value as `id` - kept as its own named field since the Candidate
    # Listing UI refers to it by this name specifically. `id` is preserved
    # unchanged for existing consumers (e.g. create_campaign_candidate).
    campaign_candidate_id: UUID | None = None
    resume_id: UUID

    pipeline_stage: PipelineStage

    # Candidate Listing UI fields (M03-E05-adjacent listing extension).
    # All read-only, sourced from existing stored data - never recalculated.
    candidate_name: str | None = None
    current_designation: str | None = None
    experience: float | None = None

    deterministic_score: float | None = None
    ai_ats_score: float | None = None
    semantic_score: float | None = None
    composite_score: float | None = None

    # Not available in the backend today - always null until a real source exists.
    location: str | None = None
    risk_score: float | None = None

    created_at: datetime

    model_config = ConfigDict(
        from_attributes=True,
    )


class CandidateScorecardResponse(CampaignCandidateResponse):
    """
    M07-E03 S03 T01: extends CampaignCandidateResponse (never duplicates
    it) with the rejection banner - used only by the single-candidate
    scorecard detail endpoint, never the campaign candidate list, so list
    consumers are entirely unaffected.

    has_rejection is only ever True when pipeline_stage == REJECTED AND
    the candidate's most recent candidate_rejections row is
    rejection_layer == DETERMINISTIC (this story's exact, explicit scope -
    a SEMANTIC/AI-layer rejection is a different epic, not surfaced here).
    """
    has_rejection: bool = False
    rejection_layer: RejectionLayer | None = None
    rejection_reason: str | None = None
    rejected_at: datetime | None = None
    score_breakdown: dict | None = None

    # Present regardless of has_rejection - hr_override can in principle
    # be set independently of this story's DETERMINISTIC-only banner scope.
    is_overridden: bool = False
    # "Overridden — Previously Rejected" when is_overridden, else None -
    # the original rejection_reason/rejected_at above are preserved
    # unchanged either way, never overwritten by the override.
    status: str | None = None


class CandidateRejectionHistoryEntryResponse(BaseModel):
    """M07-E03 S03 T02: one candidate_rejections row, read-only - no edit/delete APIs exist or are added."""
    id: UUID
    rejection_layer: RejectionLayer
    rejection_reason: str
    rejected_at: datetime
    hr_override: bool
    # 1-indexed, oldest=1 - position among this candidate's own rejection
    # history, not a stored column (candidate_rejections has no such
    # field); computed purely from rejected_at ordering.
    evaluation_round: int
    # True only for the single newest record in the list.
    current_status: bool

    model_config = ConfigDict(
        from_attributes=True,
    )


class HrOverrideRequest(BaseModel):
    """M07-E03 S04 T01: HR_ADMIN override of a deterministic rejection."""

    override_reason: str = Field(..., min_length=20)
    confirmation: bool

    @field_validator("confirmation")
    @classmethod
    def _confirmation_must_be_true(cls, value: bool) -> bool:
        if not value:
            raise ValueError("confirmation must be true to apply an HR override.")
        return value


class OverrideReportRow(BaseModel):
    """M07-E03 S04 T03: one HR override event - never includes candidate name/email/phone/resume."""

    campaign_id: UUID
    campaign_name: str
    candidate_uuid: UUID
    original_rejection_reason: str | None = None
    override_reason: str
    hr_full_name: str | None = None
    override_timestamp: datetime
    current_pipeline_stage: PipelineStage

    model_config = ConfigDict(
        from_attributes=True,
    )


class OverrideWeeklyTrendPoint(BaseModel):
    """One week's override count - Monday-anchored week_start, last 8 weeks."""

    week_start: date
    override_count: int


class CampaignOverrideAlert(BaseModel):
    """
    override_rate = overrides / rejected candidates in this campaign (%),
    all-time (not scoped to the report's date-range filter, which only
    scopes `rows`). override_alert is True when override_rate exceeds
    the OVERRIDE_RATE_ALERT_THRESHOLD platform_config key.
    """

    campaign_id: UUID
    campaign_name: str
    override_count: int
    rejected_count: int
    override_rate: float
    override_alert: bool
    recommendation: str | None = None


class OverrideReportResponse(BaseModel):
    rows: list[OverrideReportRow]
    total_count: int
    weekly_trend: list[OverrideWeeklyTrendPoint]
    campaign_alerts: list[CampaignOverrideAlert]


class RejectionBreakdownEntry(BaseModel):
    """M07-E03 S05 T01: one of the 7 mandatory/experience/education failure-combination buckets."""

    category: str
    count: int
    percentage: float


class MissingSkillOccurrence(BaseModel):
    """M07-E03 S05 T01: one canonical skill's occurrence among MISSING mandatory-skill matches."""

    canonical_name: str
    occurrence_count: int
    percentage_of_rejections: float


class JdCalibrationRecommendation(BaseModel):
    """M07-E03 S05 T02: one structured JD-calibration suggestion."""

    rule: str
    message: str
    action: str | None = None
    details: dict | None = None


class CampaignRejectionAnalyticsResponse(BaseModel):
    campaign_id: UUID
    total_candidates: int
    total_deterministic_rejections: int
    # The threshold actually used to gate `recommendations` below - read
    # from PlatformConfig, never hardcoded (M07-E03 S05 T02).
    min_candidates_for_analytics: int
    breakdown: list[RejectionBreakdownEntry]
    top_missing_skills: list[MissingSkillOccurrence]
    # Empty until total_candidates >= min_candidates_for_analytics.
    recommendations: list[JdCalibrationRecommendation]