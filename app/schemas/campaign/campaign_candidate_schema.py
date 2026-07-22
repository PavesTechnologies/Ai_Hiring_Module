from uuid import UUID

from pydantic import BaseModel, ConfigDict
from datetime import datetime

from app.models.pipeline import PipelineStage

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