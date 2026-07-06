from typing import Optional

from pydantic import BaseModel, Field


class EducationCriteria(BaseModel):
    degree: Optional[str] = None
    field: Optional[str] = None


class CreateJDRequest(BaseModel):

    title: str = Field(
        ...,
        min_length=1,
        max_length=255
    )

    raw_text: Optional[str] = Field(
        ...,
        min_length=1
    )

    jurisdiction: str

    min_experience_years: Optional[float] = None

    education_criteria: Optional[EducationCriteria] = None


class UpdateJDRequest(BaseModel):
    title: Optional[str] =Field(..., max_length=255)
    raw_text: Optional[str] = None
    jurisdiction: str
    min_experience_years: Optional[float] = None
    education_criteria: Optional[EducationCriteria] = None

class JDSearchRequest(BaseModel):
    search: Optional[str] | None
    jurisdiction: Optional[str] | None
    active: Optional[bool] = True
    source_format: Optional[str] | None

    page: int = Field(default=1, ge=1, description="Page number, must be greater than or equal to 1")
    size: int = Field(default=10, ge=1, le=100, description="Page size, must be between 1 and 100")

    sort_by: str = "create_at"
    order: str = "desc"