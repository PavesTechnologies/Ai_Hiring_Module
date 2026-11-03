"""Shadow mode (retire v1, phase D): the current vs a candidate scoring version, recruiter ranking, versions."""
from datetime import datetime

from pydantic import BaseModel, Field


class ShadowReportRow(BaseModel):
    campaign_candidate_id: str
    candidate_name: str
    stage: str
    parse_status: str | None = None
    # The current version: the result acted on. Outcomes: PASS / REVIEW / REJECT / PENDING / NOT_RUN / NOT_SCORED.
    current_score: float | None = None
    current_outcome: str
    current_outcome_reason: str | None = None
    current_rank: int | None = None
    current_core_confirmed: str | None = None
    current_failed_checks: list[str] = Field(default_factory=list)
    current_scoring_mode: str | None = None
    current_scored_at: datetime | None = None
    current_config_version: str | None = None
    # The campaign's candidate version: its latest result, never acted on.
    candidate_score: float | None = None
    candidate_outcome: str
    candidate_outcome_reason: str | None = None
    candidate_rank: int | None = None
    candidate_core_confirmed: str | None = None
    candidate_failed_checks: list[str] = Field(default_factory=list)
    candidate_scoring_mode: str | None = None
    candidate_scored_at: datetime | None = None
    candidate_config_version: str | None = None
    # Core-skill detail of the candidate version's result (else the current one's).
    core_confirmed_names: list[str] = Field(default_factory=list)
    core_uncertain: list[str] = Field(default_factory=list)
    core_found_not_confirmed: list[str] = Field(default_factory=list)
    core_rejected_by_llm: list[str] = Field(default_factory=list)
    core_sense_mismatch: list[str] = Field(default_factory=list)
    recruiter_rank: int | None = None
    recruiter_ranked_by: str | None = None
    recruiter_note: str | None = None
    disagreements: list[str] = Field(default_factory=list)


class ScoringVersionRef(BaseModel):
    id: str
    label: str
    model: str | None = None


class ShadowReportResponse(BaseModel):
    campaign_id: str
    campaign_name: str
    current_version: ScoringVersionRef | None = None
    candidate_version: ScoringVersionRef | None = None
    daily_llm_calls_used: int
    daily_llm_call_cap: int
    # Per model (the provider's free-tier daily limit is per model): requests used today and its cap.
    daily_llm_usage_by_model: list[dict] = Field(default_factory=list)
    assumed_jd_senses: list[str] = Field(default_factory=list)
    # {"current": {outcome: n}, "candidate": {outcome: n}}
    outcome_counts: dict[str, dict[str, int]] = Field(default_factory=dict)
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


class ScoringVersionCreateRequest(BaseModel):
    label: str = Field(min_length=1, max_length=100)
    # The SCORING_V2_CONFIG shape - only the keys that differ from the defaults are needed.
    configuration: dict = Field(default_factory=dict)
    # Core-confirmation model on the active provider; None = the platform's model.
    model: str | None = Field(default=None, max_length=150)
    note: str | None = Field(default=None, max_length=1000)


class ScoringVersionResponse(BaseModel):
    id: str
    label: str
    status: str
    model: str | None = None
    configuration: dict = Field(default_factory=dict)
    note: str | None = None
    created_at: datetime | None = None
    activated_at: datetime | None = None
    retired_at: datetime | None = None
    campaigns_trying: list[dict] = Field(default_factory=list)


class CandidateScoringVersionRequest(BaseModel):
    # The candidate version to try on this campaign; None stops shadow scoring.
    version_id: str | None = None
    # Score the campaign's already-scored candidates with it now (respects the daily LLM cap).
    backfill: bool = False


class CandidateScoringVersionResponse(BaseModel):
    campaign_id: str
    candidate_scoring_version: ScoringVersionResponse | None = None
    queued: int
