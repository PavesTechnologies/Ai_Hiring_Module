from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class CampaignResponse(BaseModel):
    id: UUID

    name: str

    status: str

    jd_title: str

    jd_version: int

    hiring_manager: str | None

    created_at: datetime