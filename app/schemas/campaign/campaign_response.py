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

    deadline: datetime | None

    created_at: datetime