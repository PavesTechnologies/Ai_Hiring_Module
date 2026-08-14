from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class AuditLogEntryResponse(BaseModel):
    id: UUID
    actor_id: str | None
    actor_name: str
    actor_role: str | None
    action_type: str
    entity_type: str
    entity_id: UUID
    campaign_id: UUID | None
    detail: dict | None
    created_at: datetime


class AuditLogSearchResponse(BaseModel):
    items: list[AuditLogEntryResponse]
    page: int
    page_size: int
    total: int
