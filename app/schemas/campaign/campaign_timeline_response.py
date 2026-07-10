from datetime import datetime
from uuid import UUID
from pydantic import BaseModel


class TimelineEntry(BaseModel):
    timestamp: datetime
    event_type: str
    actor_name: str
    description: str


class CampaignTimelineResponse(BaseModel):
    campaign_id: UUID
    total_events: int
    limit: int
    offset: int
    events: list[TimelineEntry]
