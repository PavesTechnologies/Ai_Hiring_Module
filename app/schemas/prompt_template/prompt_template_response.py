from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class PromptResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    task_type: str
    name: str
    template_text: str
    content_hash: str
    status: str
    notes: Optional[str]
    updated_by: Optional[str]
    updated_at: Optional[datetime]
    created_at: datetime


class PromptListResponse(BaseModel):
    items: list[PromptResponse]
    page: int
    page_size: int
    total: int


class PromptLookupResponse(BaseModel):
    """id + name only - for frontend dropdown population (JD/Campaign prompt template pickers)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
