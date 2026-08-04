from typing import Any

from pydantic import BaseModel, Field, field_validator


def _clean_string_list(values: list[str]) -> list[str]:
    """
    Case-insensitive dedupe that keeps the first (assumed most-complete)
    form seen for a given skill/certification — mirrors
    jd_extraction_response._clean_string_list's ordering guarantee, but
    folds on casing too since resume skill sections are far less
    consistently capitalized than JD skill lists.
    """
    seen = set()
    cleaned = []
    for value in values or []:
        normalized = value.strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(normalized)
    return cleaned


def _clean_optional_string(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


class WorkExperience(BaseModel):
    title: str | None = None
    company: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    is_current: bool = False
    is_internship: bool = False
    is_volunteer: bool = False
    description: str | None = None

    @field_validator("title", "company", "start_date", "end_date", "description")
    @classmethod
    def clean_optional_string(cls, value: str | None) -> str | None:
        return _clean_optional_string(value)


class ProjectEntry(BaseModel):
    name: str | None = None
    description: str | None = None
    tech: list[str] = Field(default_factory=list)

    @field_validator("name", "description")
    @classmethod
    def clean_optional_string(cls, value: str | None) -> str | None:
        return _clean_optional_string(value)

    @field_validator("tech")
    @classmethod
    def clean_tech_list(cls, values: list[str]) -> list[str]:
        return _clean_string_list(values)


class EducationEntry(BaseModel):
    degree: str | None = None
    institution: str | None = None
    field: str | None = None
    graduation_year: int | None = None

    @field_validator("degree", "institution", "field")
    @classmethod
    def clean_optional_string(cls, value: str | None) -> str | None:
        return _clean_optional_string(value)


class ResumeExtractionResponse(BaseModel):
    # Email/phone must never appear here or in resumes.parsed_json --
    # PIIDetectionService/PIIRedactionService (app/services/pii/) own those
    # deterministically, redacting them out of the text before this
    # extraction call ever runs. full_name is the one identity field this
    # schema does carry: the pipeline redacts contact info but deliberately
    # leaves the candidate's name visible to the model, and the bulk-ZIP
    # upload flow (which has no upload form to source identity from) reads
    # it from here for Candidate creation -- no separate identity-only AI
    # call exists anymore.
    full_name: str | None = None
    skills: list[str] = Field(default_factory=list)
    # Non-technical/behavioral skills (e.g. "Communication", "Leadership"),
    # kept separate from `skills` so they never reach
    # SkillNormalizationService's skill-ontology matching/scoring pipeline -
    # display-only, empty list if the resume has none.
    soft_skills: list[str] = Field(default_factory=list)
    work_experience: list[WorkExperience] = Field(default_factory=list)
    education: list[EducationEntry] = Field(default_factory=list)
    projects: list[ProjectEntry] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    total_experience_years: float | None = None
    department: str | None = None
    location: str | None = None
    summary: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("skills", "soft_skills", "certifications")
    @classmethod
    def clean_lists(cls, values: list[str]) -> list[str]:
        return _clean_string_list(values)

    @field_validator("full_name", "department", "location", "summary")
    @classmethod
    def clean_optional_string(cls, value: str | None) -> str | None:
        return _clean_optional_string(value)

    @field_validator("total_experience_years")
    @classmethod
    def validate_total_experience_years(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError("total_experience_years cannot be negative")
        return value


class ResumeExtractionGenerationSchema(BaseModel):
    """
    Same shape as ResumeExtractionResponse minus `metadata` - Gemini's
    Developer API mode rejects open-ended dict fields (they compile to a
    JSON Schema `additionalProperties`, which that mode doesn't support)
    when used as a response_schema for structured output. metadata is
    always {} per the prompt anyway, and ResumeExtractionResponse.metadata
    defaults to {} when the key is absent, so dropping it here only affects
    generation, not parsing. Mirrors JDExtractionGenerationSchema.
    """
    full_name: str | None = None
    skills: list[str] = Field(default_factory=list)
    soft_skills: list[str] = Field(default_factory=list)
    work_experience: list[WorkExperience] = Field(default_factory=list)
    education: list[EducationEntry] = Field(default_factory=list)
    projects: list[ProjectEntry] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    total_experience_years: float | None = None
    department: str | None = None
    location: str | None = None
    summary: str | None = None
