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

    raw_text: str = Field(
        ...,
        min_length=1
    )

    jurisdiction: str

    min_experience_years: Optional[float] = None

    education_criteria: Optional[EducationCriteria] = None