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
    deadline: datetime | None
    created_at: datetime