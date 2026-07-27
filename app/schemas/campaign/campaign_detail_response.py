from datetime import datetime
from uuid import UUID
from pydantic import BaseModel

class CampaignInfoSection(BaseModel):
    name: str
    status: str
    created_by_name: str | None
    created_at: datetime
    updated_at: datetime | None
    duplicated_from_campaign_id: UUID | None = None
    duplicated_from_campaign_name: str | None = None

class JDConfigSection(BaseModel):
    jd_id: UUID
    jd_title: str
    version_number: int
    jurisdiction: str | None
    mandatory_skill_count: int

class ScoringConfigSection(BaseModel):
    weight_deterministic: float
    weight_semantic: float
    weight_ai: float
    semantic_threshold: float
    ai_threshold: float
    deterministic_threshold: float

class PipelineLimitsSection(BaseModel):
    max_candidates: int | None
    current_candidate_count: int
    deadline: datetime | None

class HiringManagerSection(BaseModel):
    full_name: str
    email: str

class CampaignDetailResponse(BaseModel):
    id: UUID
    campaign_info: CampaignInfoSection
    jd_configuration: JDConfigSection
    scoring_configuration: ScoringConfigSection | None = None # hidden for HM
    pipeline_limits: PipelineLimitsSection
    hiring_manager: HiringManagerSection | None = None # hidden for HM