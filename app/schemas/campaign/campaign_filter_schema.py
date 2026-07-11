from uuid import UUID

from pydantic import BaseModel

from app.models.campaigns import CampaignStatus


class CampaignFilterRequest(BaseModel):
    search: str | None = None
    status: CampaignStatus | None = None
    hiring_manager_id: str | None = None
    jd_id: UUID | None = None
    has_deadline: bool | None = None
    show_closed: bool = False