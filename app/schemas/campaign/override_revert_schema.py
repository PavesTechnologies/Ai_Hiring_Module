from uuid import UUID

from pydantic import BaseModel, Field


class OverrideRevertRequest(BaseModel):
    """M11-E04-S02-T03 — clearing an override needs its own justification."""

    reason: str = Field(..., min_length=10, max_length=1000)


class OverrideRevertResultResponse(BaseModel):
    campaign_candidate_id: UUID
    pipeline_stage: str
    # What the candidate was put back to — surfaced so the UI can say which
    # layer's decision is now in force, not just "reverted".
    restored_decision_type: str
    restored_decision_source: str
    restored_decision_reason: str | None = None
    # The override text being discarded, echoed back for the confirmation UI.
    cleared_override_reason: str | None = None
    detail: str
