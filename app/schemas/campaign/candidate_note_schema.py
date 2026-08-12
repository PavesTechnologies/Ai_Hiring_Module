from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class CandidateNoteCreateRequest(BaseModel):
    """M11-E04-S01-T01."""

    note_text: str = Field(..., min_length=1, max_length=5000)


class CandidateNoteUpdateRequest(BaseModel):
    """M11-E04-S01-T02."""

    note_text: str = Field(..., min_length=1, max_length=5000)


class CandidateNoteResponse(BaseModel):
    id: UUID
    campaign_candidate_id: UUID
    note_text: str
    created_by: str
    # Resolved from users; falls back to the raw id if the lookup fails, so a
    # name-service problem never blanks out the author.
    created_by_name: str
    created_at: datetime
    updated_at: datetime | None = None
    is_edited: bool = False


class CandidateNoteCountsRequest(BaseModel):
    """T03 — ask for a whole page of candidates at once."""

    campaign_candidate_ids: list[UUID] = Field(..., min_length=1, max_length=500)


class CandidateNoteCountsResponse(BaseModel):
    """T03 — {campaign_candidate_id: note_count} for a page of candidates."""

    counts: dict[str, int]
