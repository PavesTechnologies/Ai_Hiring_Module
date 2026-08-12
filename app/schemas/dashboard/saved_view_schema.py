from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class SavedViewCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    # Whole filter + sort state, stored verbatim. Kept schemaless because the
    # E03 filter set is still growing (M11-E03-S02/S03).
    filters: dict = Field(default_factory=dict)


class SavedViewUpdateRequest(BaseModel):
    """Any omitted field is left unchanged."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    filters: dict | None = None


class SavedViewResponse(BaseModel):
    id: UUID
    campaign_id: UUID
    name: str
    description: str | None = None
    filters: dict
    last_applied_at: datetime | None = None
    created_at: datetime
    updated_at: datetime | None = None


class SkillFilterResultResponse(BaseModel):
    """
    Result of a multi-skill AND search (M11-E03-S01-T02). Returns ids rather
    than full candidate rows so the caller can intersect this with whatever
    other filters are already applied, without re-fetching candidates.
    """

    campaign_candidate_ids: list[UUID]
    # campaign_candidate_id -> the match_tier of each searched skill
    match_tiers: dict[str, list[str]]
    result_count: int


class CandidateFilterResultResponse(BaseModel):
    """
    Result of the resume-derived filters (M11-E03-S02-T02/T03). ids only, so
    the caller intersects them with whatever else is already applied.
    """

    campaign_candidate_ids: list[UUID]
    result_count: int


class CampaignUploaderResponse(BaseModel):
    """One entry for the 'Uploaded By' dropdown (M11-E03-S02-T03)."""

    user_id: str
    full_name: str
    upload_count: int


class CandidateCampaignAppearanceResponse(BaseModel):
    """One campaign a cross-campaign search hit appears in."""

    campaign_id: UUID
    campaign_candidate_id: UUID
    campaign_name: str
    campaign_status: str
    jd_title: str | None = None
    pipeline_stage: str
    composite_score: float | None = None


class CrossCampaignCandidateResponse(BaseModel):
    """
    One candidate, deduplicated across every campaign they appear in
    (M11-E03-S04). No PII — candidate UUID only.
    """

    candidate_id: UUID
    best_composite_score: float | None = None
    best_campaign_name: str | None = None
    appearances: list[CandidateCampaignAppearanceResponse]
    # true when every appearance is REJECTED — these are the candidates
    # genuinely available to consider for a new campaign
    rejected_everywhere: bool


class CrossCampaignSearchResponse(BaseModel):
    results: list[CrossCampaignCandidateResponse]
    result_count: int


class SkillSuggestionResponse(BaseModel):
    """One autocomplete hit (M11-E03-S01-T01)."""

    canonical_skill_id: UUID
    canonical_name: str
    category: str | None = None
    # how many candidates in THIS campaign hold the skill — lets the UI show
    # "Python (14)" so a user can tell a useful filter from a dead one
    candidate_count: int
