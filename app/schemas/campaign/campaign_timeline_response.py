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
    # Distinct event types present in this campaign's FULL (unfiltered)
    # timeline — the frontend builds its filter dropdown from this, so the
    # options can never go stale or offer a type with zero matches.
    available_event_types: list[str] = []
