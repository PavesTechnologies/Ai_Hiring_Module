from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class EducationFieldRelationItem(BaseModel):
    id: UUID
    field_a: str
    field_b: str
    role_domain: str
    note: str | None
    created_by: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class EducationFieldRelationList(BaseModel):
    relations: list[EducationFieldRelationItem]
    # What a pair can name, for the editor.
    fields: list[str]
    role_domains: list[str]


class EducationFieldRelationCreate(BaseModel):
    field_1: str = Field(..., min_length=1, max_length=40)
    field_2: str = Field(..., min_length=1, max_length=40)
    role_domain: str = Field(..., min_length=1, max_length=20)
    note: str | None = Field(default=None, max_length=500)
