from typing import Any

from pydantic import BaseModel, Field, field_validator


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


class ResumeExtractionResponse(BaseModel):
    """
    Raw structured shape extracted from a resume. Deliberately looser than
    JDExtractionResponse — there's no per-field persistence for resumes yet
    (Resume.parsed_json is a single JSONB blob), so this only exists to
    give the AI_EXTRACTION -> JSON_VALIDATION stage boundary something
    concrete to validate rather than accepting an arbitrary dict.
    """

    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    skills: list[str] = Field(default_factory=list)
    total_experience_years: float | None = None
    education: list[dict[str, Any]] = Field(default_factory=list)
    work_experience: list[dict[str, Any]] = Field(default_factory=list)
    summary: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("skills")
    @classmethod
    def clean_skills(cls, value: list[str]) -> list[str]:
        return _clean_string_list(value)

    @field_validator("full_name", "email", "phone", "summary")
    @classmethod
    def clean_optional_string(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("total_experience_years")
    @classmethod
    def validate_years(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError("total_experience_years cannot be negative")
        return value
