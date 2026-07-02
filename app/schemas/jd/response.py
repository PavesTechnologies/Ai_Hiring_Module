from uuid import UUID

from pydantic import BaseModel
from datetime import datetime


class CreateJDResponse(BaseModel):

    id: UUID

    title: str

    version_number: int

    source_format: str

    jurisdiction: str
    

class GetJDResponse(BaseModel):
    id: UUID
    title: str
    raw_text: str
    parsed_skills: dict | None
    required_skills: dict | None
    min_experience_years: float | None
    education_criteria: dict | None
    source_format: str
    jurisdiction: str | None
    version_number: int
    is_active_version: bool
    created_by: str
    created_at: datetime
    updated_at: datetime | None
    
class UpdateJDResponse(BaseModel):
    id: UUID
    title: str
    version_number: int
    message: str
    
class JDListItem(BaseModel):
    id: UUID
    title: str
    version_number: int
    jurisdiction: str | None
    source_format: str
    created_at: datetime
    

class PaginatedJDResponse(BaseModel):
    total: int
    page: int
    size: int
    items: list[JDListItem]