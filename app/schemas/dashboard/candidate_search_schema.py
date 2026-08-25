from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


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


class SkillSuggestionResponse(BaseModel):
    """One autocomplete hit (M11-E03-S01-T01)."""

    canonical_skill_id: UUID
    canonical_name: str
    category: str | None = None
    # how many candidates in THIS campaign hold the skill — lets the UI show
    # "Python (14)" so a user can tell a useful filter from a dead one
    candidate_count: int
