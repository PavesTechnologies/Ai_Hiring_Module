from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class UnknownSkillResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    skill_name: str
    status: str
    created_at: datetime
    updated_at: datetime


class UnknownSkillPageResponse(BaseModel):
    items: list[UnknownSkillResponse]
    page: int
    page_size: int
    total: int
