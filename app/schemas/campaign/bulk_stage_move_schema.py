from uuid import UUID

from pydantic import BaseModel, Field, field_validator


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
    """M11-E04-S03-T01 — move one candidate; the reason follows the table."""

    target_stage: str = Field(..., description="Must be a legal transition from the candidate's current stage.")
    # Optional here on purpose: whether a reason is mandatory is a property of
    # the specific transition (allowed_transitions.requires_reason), not of
    # every single move - PipelineTransitionService raises
    # PipelineTransitionReasonRequiredException (422) when the governing row
    # demands one and none was given. Making it unconditionally required at
    # this layer instead rejected reason-free moves (e.g. SHORTLISTED ->
    # HM_REVIEW) with a 422 before that rule was ever consulted, and made the
    # requires_reason flag on the allowed-transitions endpoint unusable.
    # /bulk-stage-move stays mandatory - a batch carries one shared
    # justification regardless of which transition it applies.
    reason: str | None = Field(default=None, min_length=10, max_length=1000)

    @field_validator("reason", mode="before")
    @classmethod
    def _blank_reason_is_none(cls, value):
        """
        The UI sends reason="" for a transition whose allowed_transitions row
        has requires_reason=False. Treated as "not supplied" rather than as a
        0-character string, so it reaches the per-transition rule instead of
        failing min_length here - which is the same 422-before-the-real-rule
        problem the optional field above exists to avoid.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value


class ManualRejectRequest(BaseModel):
    """M11-E04-S03-T03 — reject one candidate straight from the list."""

    reason: str = Field(..., min_length=10, max_length=1000)


class SingleStageMoveResultResponse(BaseModel):
    campaign_candidate_id: UUID
    from_stage: str
    to_stage: str
    detail: str


class AllowedTransitionResponse(BaseModel):
    """One legal next stage for a specific candidate, for the caller's role."""

    to_stage: str
    # True when the governing allowed_transitions row sets requires_reason -
    # the UI must collect a reason before POSTing the move (the move endpoint
    # rejects it otherwise).
    requires_reason: bool
    notes: str | None = None


class CandidateAllowedTransitionsResponse(BaseModel):
    campaign_candidate_id: UUID
    current_stage: str
    previous_stage: str | None = None
    # Only targets this caller may actually move the candidate to - role-denied
    # and SYSTEM-only rows are filtered out, never returned disabled.
    allowed_transitions: list[AllowedTransitionResponse]
