from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.enums.education import DegreeLevel, EducationField


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


class RequiredSkillItem(BaseModel):
    """
    A required (mandatory) JD skill plus its AI-classified importance.
    `importance` is required here (not Optional) — the JD_PARSE prompt is
    instructed to fall back to "supporting" itself whenever it's unsure,
    so an ambiguous case never reaches this schema as a missing value.
    """
    name: str
    importance: Literal["core", "supporting"]

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("required skill name cannot be empty")
        return value


class PreferredSkillItem(BaseModel):
    """
    A preferred (non-scoring) JD skill. Deliberately has no `importance`
    field — preferred skills never participate in the required-skill
    qualification score, so there is nothing for the AI to classify.
    """
    name: str

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("preferred skill name cannot be empty")
        return value


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
    # AI-classified, controlled-vocabulary companions to the raw degree/field
    # text above — see app/enums/education.py for the vocabularies. Default
    # to UNKNOWN (never guessed) rather than None, so downstream code always
    # gets a valid enum value to branch on. Note: this Education object is
    # the raw AI-extracted JD education (kept for extracted_json parity) —
    # it is NOT the same as JobDescription.education_criteria, which is a
    # separate, recruiter-typed field that actually drives education
    # matching (see EducationMatchingService for how that free text gets
    # normalized instead).
    degree_level: str = "UNKNOWN"
    field_normalized: str = "UNKNOWN"
    related_field_allowed: bool = False

    @field_validator("degree", "field")
    @classmethod
    def clean_optional_string(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("degree_level")
    @classmethod
    def validate_degree_level(cls, value: str) -> str:
        try:
            return DegreeLevel(value).value
        except ValueError:
            return DegreeLevel.UNKNOWN.value

    @field_validator("field_normalized")
    @classmethod
    def validate_field_normalized(cls, value: str) -> str:
        try:
            return EducationField(value).value
        except ValueError:
            return EducationField.UNKNOWN.value


class JDExtractionResponse(BaseModel):
    required_skills: list[RequiredSkillItem] = Field(default_factory=list)
    preferred_skills: list[PreferredSkillItem] = Field(default_factory=list)
    # Non-technical/behavioral skills (e.g. "Communication", "Leadership"),
    # kept separate from required_skills/preferred_skills so they never
    # reach SkillNormalizationService's skill-ontology matching/scoring
    # pipeline - display-only, empty list if the JD has none.
    soft_skills: list[str] = Field(default_factory=list)
    responsibilities: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    experience: Experience | None = None
    education: Education | None = None
    employment_type: str | None = None
    work_mode: str | None = None
    location: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("soft_skills", "responsibilities", "certifications")
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
    def dedupe_skill_lists(self) -> "JDExtractionResponse":
        """
        Required wins: a skill named in both lists is kept only under
        required_skills (with whatever importance the AI gave it there).
        Also drops same-list duplicate names, first occurrence wins -
        replaces the old _clean_string_list dedupe, which only worked on
        plain strings.
        """
        seen_required: set[str] = set()
        deduped_required: list[RequiredSkillItem] = []
        for item in self.required_skills:
            if item.name in seen_required:
                continue
            seen_required.add(item.name)
            deduped_required.append(item)
        self.required_skills = deduped_required

        seen_preferred: set[str] = set()
        deduped_preferred: list[PreferredSkillItem] = []
        for item in self.preferred_skills:
            if item.name in seen_required or item.name in seen_preferred:
                continue
            seen_preferred.add(item.name)
            deduped_preferred.append(item)
        self.preferred_skills = deduped_preferred
        return self


class JDExtractionGenerationSchema(BaseModel):
    """
    Same shape as JDExtractionResponse minus `metadata` - Gemini's Developer
    API mode rejects open-ended dict fields (they compile to a JSON Schema
    `additionalProperties`, which that mode doesn't support) when used as a
    response_schema for structured output. metadata is always {} per the
    prompt anyway, and JDExtractionResponse.metadata defaults to {} when the
    key is absent, so dropping it here only affects generation, not parsing.
    """
    required_skills: list[RequiredSkillItem] = Field(default_factory=list)
    preferred_skills: list[PreferredSkillItem] = Field(default_factory=list)
    soft_skills: list[str] = Field(default_factory=list)
    responsibilities: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    experience: Experience | None = None
    education: Education | None = None
    employment_type: str | None = None
    work_mode: str | None = None
    location: str | None = None
