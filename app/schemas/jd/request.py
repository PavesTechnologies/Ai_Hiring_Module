from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.models.jd.job_descriptions import JDVerificationStatus


class EducationCriteria(BaseModel):
    degree: Optional[str] = None
    field: Optional[str] = None


def _validate_experience_range(request: BaseModel) -> BaseModel:
    if (
        request.min_experience_years is not None
        and request.max_experience_years is not None
        and request.min_experience_years > request.max_experience_years
    ):
        raise ValueError("min_experience_years cannot exceed max_experience_years")
    return request


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

    min_experience_years: float = Field(...)

    max_experience_years: float = Field(...)

    notice_period: int = Field(...)

    education_criteria: EducationCriteria = Field(...)

    prompt_template_id: UUID = Field(..., description="Must reference an ACTIVE JD_PARSE prompt template.")

    _validate_experience_range = model_validator(mode="after")(_validate_experience_range)


class UpdateJDRequest(BaseModel):
    title: Optional[str] =Field(..., max_length=255)
    raw_text: Optional[str] = None
    jurisdiction: str
    min_experience_years: float = Field(...)
    max_experience_years: float = Field(...)
    notice_period: int = Field(...)
    education_criteria: EducationCriteria = Field(...)

    prompt_template_id: UUID = Field(..., description="Must reference an ACTIVE JD_PARSE prompt template.")

    _validate_experience_range = model_validator(mode="after")(_validate_experience_range)

class JDSearchRequest(BaseModel):
    search: Optional[str] | None
    jurisdiction: Optional[str] | None
    active: Optional[bool] = True
    source_format: Optional[str] | None
    is_verified: Optional[JDVerificationStatus] | None

    page: int = Field(default=1, ge=1, description="Page number, must be greater than or equal to 1")
    size: int = Field(default=10, ge=1, le=100, description="Page size, must be between 1 and 100")

    sort_by: str = "create_at"
    order: str = "desc"