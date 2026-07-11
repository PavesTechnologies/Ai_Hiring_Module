from uuid import UUID

from pydantic import BaseModel
from datetime import datetime


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
    updated_by: str

class JDListItem(BaseModel):
    id: UUID
    title: str
    version_number: int
    jurisdiction: str | None
    source_format: str
    created_by: str
    created_at: datetime
    

class PaginatedJDResponse(BaseModel):
    total: int
    page: int
    size: int
    items: list[JDListItem]


class JDProcessingAcceptedResponse(BaseModel):
    task_id: UUID
    status: str


class StageProgress(BaseModel):
    stage: str
    status: str
    error_message: str | None
    duration_ms: int | None


class JDProcessingStatusResponse(BaseModel):
    task_id: UUID
    overall_status: str
    current_stage: str | None
    stages: list[StageProgress]
    jd_id: UUID | None
    error_message: str | None