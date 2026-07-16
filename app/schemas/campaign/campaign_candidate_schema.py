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
    resume_id: UUID

    pipeline_stage: PipelineStage

    created_at: datetime

    model_config = ConfigDict(
        from_attributes=True,
    )