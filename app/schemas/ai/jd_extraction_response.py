from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Any


def _clean_string_list(values: list[str]) -> list[str]:
    seen = set()
    cleaned = []
    for value in values or []:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        cleaned.append(normalized)
    return cleaned


class Experience(BaseModel):
    min_experience_years: float | None = None
    max_experience_years: float | None = None

    @field_validator("min_experience_years", "max_experience_years")
    @classmethod
    def validate_years(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError("experience years cannot be negative")
        return value

    @model_validator(mode="after")
    def validate_range(self) -> "Experience":
        if (
            self.min_experience_years is not None
            and self.max_experience_years is not None
            and self.min_experience_years > self.max_experience_years
        ):
            raise ValueError("min_experience_years cannot exceed max_experience_years")
        return self

class Education(BaseModel):
    degree: str | None = None
    field: str | None = None

    @field_validator("degree", "field")
    @classmethod
    def clean_optional_string(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None
    
class JDExtractionResponse(BaseModel):
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    responsibilities: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    experience: Experience | None = None
    education: Education | None = None
    employment_type: str | None = None
    work_mode: str | None = None
    location: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("required_skills", "preferred_skills", "responsibilities", "certifications")
    @classmethod
    def clean_lists(cls, values: list[str]) -> list[str]:
        return _clean_string_list(values)

    @field_validator("employment_type", "work_mode", "location")
    @classmethod
    def clean_optional_string(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @model_validator(mode="after")
    def dedupe_preferred_against_required(self) -> "JDExtractionResponse":
        # Required wins: a skill listed as both required and preferred is
        # kept only under required_skills.
        required_set = set(self.required_skills)
        self.preferred_skills = [
            skill for skill in self.preferred_skills if skill not in required_set
        ]
        return self
