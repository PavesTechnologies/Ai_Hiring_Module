"""Scoring v2 Step 4: shadow report, recruiter ranking and shadow switch."""
from datetime import datetime

from pydantic import BaseModel, Field


class ShadowReportRow(BaseModel):
    campaign_candidate_id: str
    candidate_name: str
    stage: str
    parse_status: str | None = None
    v1_score: float | None = None
    v1_outcome: str
    v1_rank: int | None = None
    v2_score: float | None = None
    # PASS / REVIEW / REJECT / PENDING / NOT_RUN / NOT_SCORED
    v2_outcome: str
    v2_outcome_reason: str | None = None
    v2_rank: int | None = None
    v2_core_confirmed: str | None = None
    v2_core_confirmed_names: list[str] = Field(default_factory=list)
    v2_core_uncertain: list[str] = Field(default_factory=list)
    v2_core_found_not_confirmed: list[str] = Field(default_factory=list)
    v2_core_rejected_by_llm: list[str] = Field(default_factory=list)
    v2_core_sense_mismatch: list[str] = Field(default_factory=list)
    v2_failed_checks: list[str] = Field(default_factory=list)
    v2_scored_at: datetime | None = None
    v2_config_version: str | None = None
    recruiter_rank: int | None = None
    recruiter_ranked_by: str | None = None
    recruiter_note: str | None = None
    disagreements: list[str] = Field(default_factory=list)


class ShadowReportResponse(BaseModel):
    campaign_id: str
    campaign_name: str
    scoring_v2_shadow: bool
    scoring_v2_enabled: bool
    daily_llm_calls_used: int
    daily_llm_call_cap: int
    assumed_jd_senses: list[str] = Field(default_factory=list)
    v2_outcome_counts: dict[str, int] = Field(default_factory=dict)
    disagreement_count: int = 0
    rows: list[ShadowReportRow] = Field(default_factory=list)


class RecruiterRankingItem(BaseModel):
    campaign_candidate_id: str
    rank: int | None = Field(default=None, ge=1)
    note: str | None = Field(default=None, max_length=500)


class RecruiterRankingRequest(BaseModel):
    # Whose ranking this is (e.g. the hiring lead), if not the person entering it.
    ranked_by_name: str | None = Field(default=None, max_length=150)
    rankings: list[RecruiterRankingItem]


class ShadowSwitchRequest(BaseModel):
    enabled: bool
    # Queue v2 for candidates already scored by v1 (respects the daily LLM cap).
    backfill: bool = False


class ShadowSwitchResponse(BaseModel):
    scoring_v2_shadow: bool
    queued: int
