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
    created_by: UUID
    created_at: datetime
    updated_at: datetime | None