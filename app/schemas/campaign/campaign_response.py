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
    hiring_manager: str | None
    max_candidates: int | None
    candidate_count: int 
    shortlisted_count: int 
    deadline: datetime | None
    created_at: datetime
    approaching_cap: bool = False
    deadline_soon: bool = False