from uuid import UUID

from pydantic import BaseModel, Field


class BulkStageMoveRequest(BaseModel):
    """M11-E04-S03-T02 — move several candidates to the same next stage."""

    campaign_candidate_ids: list[UUID] = Field(..., min_length=1, max_length=200)
    target_stage: str = Field(..., description="Must be a legal transition from the shared current stage.")
    # Spec requires a shared justification; every bulk move is auditable.
    reason: str = Field(..., min_length=10, max_length=1000)


class BulkStageMoveResultResponse(BaseModel):
    moved_count: int
    from_stage: str
    to_stage: str
    # ids that were skipped and why - a partial success must be visible rather
    # than silently reported as a full one
    skipped: list[dict] = []
    detail: str


class SingleStageMoveRequest(BaseModel):
    """M11-E04-S03-T01 — move one candidate, with the same mandatory reason."""

    target_stage: str = Field(..., description="Must be a legal transition from the candidate's current stage.")
    reason: str = Field(..., min_length=10, max_length=1000)


class ManualRejectRequest(BaseModel):
    """M11-E04-S03-T03 — reject one candidate straight from the list."""

    reason: str = Field(..., min_length=10, max_length=1000)


class SingleStageMoveResultResponse(BaseModel):
    campaign_candidate_id: UUID
    from_stage: str
    to_stage: str
    detail: str
