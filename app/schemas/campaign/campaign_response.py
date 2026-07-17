from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class CampaignResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True) 
    id: UUID
    name: str
    status: str
    jd_title: str
    jd_version: int
    max_candidates: int
    hiring_manager: str | None
    max_candidates: int | None
    candidate_count: int 
    shortlisted_count: int 
    deadline: datetime | None
    created_at: datetime
    approaching_cap: bool = False
    deadline_soon: bool = False

class CampaignScoringDefaultsResponse(BaseModel):
    weight_deterministic: float
    weight_semantic: float
    weight_ai: float
    semantic_threshold: float
    ai_threshold: float

class ScoringLayerExplanationResponse(BaseModel):
    layer: str
    weight: float
    threshold: float | None
    description: str

class CampaignScoringConfigurationResponse(BaseModel):
    weight_deterministic: float
    weight_semantic: float
    weight_ai: float

    semantic_threshold: float
    ai_threshold: float
    deterministic_threshold: float
    total_weight: float
    formula:str
    layers: list[ScoringLayerExplanationResponse]
    defaults: CampaignScoringDefaultsResponse

class WeightHistoryItemResponse(BaseModel):
    changed_by: str
    changed_at: datetime
    before: dict
    after: dict

class CampaignWeightHistoryResponse(BaseModel):
    history: list[WeightHistoryItemResponse]


class HiringCampaignResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    campaign_id: UUID
    campaign_name: str
    status: str
    jd_title: str
    jd_version: int
    max_candidates: int
    hiring_manager: str | None
    max_candidates: int | None
    candidate_count: int
    shortlisted_count: int
    deadline: datetime | None