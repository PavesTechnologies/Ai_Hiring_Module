from pydantic import BaseModel, Field
from typing import Any

class Experience(BaseModel):
    min_experience_years: float | None = None
    max_experience_years: float | None = None

class Education(BaseModel):
    degree: str | None = None
    field: str | None = None
    
class JDExtractionResponse(BaseModel):
    skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    responsibilities: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    experience: Experience | None = None
    education: Education | None = None
    employee_type: str | None = None
    work_mode: str | None = None
    location: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)