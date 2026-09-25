from dataclasses import dataclass, field as dataclass_field
from typing import Any, Literal

from pydantic import BaseModel, Field, field_serializer, field_validator, model_validator

from app.enums.education import DegreeLevel, EducationField


def _clean_string_list(values: list[str] | None) -> list[str]:
    """Strip, drop empties, and drop case-insensitive duplicates (first spelling wins)."""
    seen: set[str] = set()
    cleaned = []
    for value in values or []:
        normalized = (value or "").strip() if isinstance(value, str) else ""
        if not normalized or normalized.casefold() in seen:
            continue
        seen.add(normalized.casefold())
        cleaned.append(normalized)
    return cleaned


@dataclass(frozen=True)
class JDSkillSpec:
    """
    Flat view of one extracted JD skill, as consumed by
    SkillNormalizationService (duck-typed on .name/.importance/.aliases).
    importance is None for preferred skills.
    """
    name: str
    importance: Literal["core", "supporting"] | None
    aliases: tuple[str, ...] = dataclass_field(default_factory=tuple)


class RequiredSkills(BaseModel):
    core: list[str] = Field(default_factory=list)
    supporting: list[str] = Field(default_factory=list)

    @field_validator("core", "supporting", mode="before")
    @classmethod
    def clean_lists(cls, values: list[str] | None) -> list[str]:
        return _clean_string_list(values)


class DomainCapabilities(BaseModel):
    # Business processes / functional areas (e.g. "Demand Management",
    # "Financial Planning") - never skills and never a hard gate; scored
    # against resume experience text by DomainCapabilityMatchingService.
    required: list[str] = Field(default_factory=list)
    preferred: list[str] = Field(default_factory=list)

    @field_validator("required", "preferred", mode="before")
    @classmethod
    def clean_lists(cls, values: list[str] | None) -> list[str]:
        return _clean_string_list(values)


class SkillAliasEntry(BaseModel):
    """
    Generation-only form of one `aliases` entry. Gemini's Developer API
    rejects open-ended dict fields in a response_schema, so the model emits
    a list of these and JDExtractionResponse folds it into the
    {"skill": [aliases]} map that gets stored.
    """
    skill: str
    aliases: list[str] = Field(default_factory=list)


class Experience(BaseModel):
    # Declared float (this is also the LLM response_schema - keep it a plain
    # number type); whole values are stored as int so extracted_json reads
    # 6, not 6.0.
    min_experience_years: float | None = None
    max_experience_years: float | None = None

    @field_validator("min_experience_years", "max_experience_years")
    @classmethod
    def validate_years(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError("experience years cannot be negative")
        return value

    @field_serializer("min_experience_years", "max_experience_years")
    def serialize_years(self, value: float | None) -> float | int | None:
        if value is not None and float(value).is_integer():
            return int(value)
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


def _coerce_legacy_shapes(data: dict) -> dict:
    """
    Accepts the pre-2026-09 list-of-objects skill shape
    ([{"name", "importance", "aliases"?}]) and the generation-time aliases
    list ([{"skill", "aliases"}]), and rewrites both into the stored shape.
    """
    data = dict(data)
    aliases: dict[str, list[str]] = {}

    raw_aliases = data.get("aliases")
    if isinstance(raw_aliases, dict):
        aliases.update({key: list(value or []) for key, value in raw_aliases.items()})
    elif isinstance(raw_aliases, list):
        for entry in raw_aliases:
            entry = entry.model_dump() if isinstance(entry, BaseModel) else entry
            if isinstance(entry, dict) and entry.get("skill"):
                aliases.setdefault(entry["skill"], []).extend(entry.get("aliases") or [])

    def _item_name(item) -> str | None:
        if isinstance(item, str):
            return item
        if isinstance(item, dict):
            if item.get("aliases") and item.get("name"):
                aliases.setdefault(item["name"], []).extend(item["aliases"])
            return item.get("name")
        return None

    raw_required = data.get("required_skills")
    if isinstance(raw_required, list):
        core, supporting = [], []
        for item in raw_required:
            name = _item_name(item)
            if not name:
                continue
            is_core = isinstance(item, dict) and item.get("importance") == "core"
            (core if is_core else supporting).append(name)
        data["required_skills"] = {"core": core, "supporting": supporting}

    raw_preferred = data.get("preferred_skills")
    if isinstance(raw_preferred, list):
        data["preferred_skills"] = [name for name in map(_item_name, raw_preferred) if name]

    data["aliases"] = aliases
    return data


class JDExtractionResponse(BaseModel):
    required_skills: RequiredSkills = Field(default_factory=RequiredSkills)
    preferred_skills: list[str] = Field(default_factory=list)
    # {skill name: [former names / alternative names the JD gives for it]}
    # e.g. {"ServiceNow SPM": ["ITBM", "PPM"]}. Keys are always an extracted
    # skill's exact name.
    aliases: dict[str, list[str]] = Field(default_factory=dict)
    domain_capabilities: DomainCapabilities = Field(default_factory=DomainCapabilities)
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

    @model_validator(mode="before")
    @classmethod
    def coerce_input_shapes(cls, data: Any) -> Any:
        return _coerce_legacy_shapes(data) if isinstance(data, dict) else data

    @field_validator("preferred_skills", "soft_skills", "responsibilities", "certifications", mode="before")
    @classmethod
    def clean_lists(cls, values: list[str] | None) -> list[str]:
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
        Enforces the invariants the matching layer relies on (all
        comparisons case-insensitive):
        - one skill appears once: core > supporting > preferred.
        - a term that is another skill's alias is never a separate skill.
        - a term extracted as a domain capability is never also a skill.
        - aliases keys are extracted skills; values exclude the key itself.
        - a required domain capability is never repeated as preferred.
        """
        capability_keys = {
            value.casefold()
            for value in self.domain_capabilities.required + self.domain_capabilities.preferred
        }
        alias_owner = {
            alias.casefold(): owner.casefold()
            for owner, values in self.aliases.items()
            for alias in values
            if alias.casefold() != owner.casefold()
        }
        seen: set[str] = set()

        def _keep(name: str) -> bool:
            key = name.casefold()
            owner = alias_owner.get(key)
            if key in seen or key in capability_keys or (owner is not None and owner != key):
                return False
            seen.add(key)
            return True

        self.required_skills.core = [name for name in self.required_skills.core if _keep(name)]
        self.required_skills.supporting = [name for name in self.required_skills.supporting if _keep(name)]
        self.preferred_skills = [name for name in self.preferred_skills if _keep(name)]

        skill_by_key = {name.casefold(): name for name in self._all_skill_names()}
        cleaned_aliases: dict[str, list[str]] = {}
        for owner, values in self.aliases.items():
            skill_name = skill_by_key.get(owner.strip().casefold())
            if skill_name is None:
                continue
            merged = _clean_string_list(cleaned_aliases.get(skill_name, []) + list(values))
            merged = [alias for alias in merged if alias.casefold() != skill_name.casefold()]
            if merged:
                cleaned_aliases[skill_name] = merged
        self.aliases = cleaned_aliases

        required_capability_keys = {value.casefold() for value in self.domain_capabilities.required}
        self.domain_capabilities.preferred = [
            value for value in self.domain_capabilities.preferred
            if value.casefold() not in required_capability_keys
        ]
        return self

    def _all_skill_names(self) -> list[str]:
        return self.required_skills.core + self.required_skills.supporting + self.preferred_skills

    def required_skill_specs(self) -> list[JDSkillSpec]:
        return [
            JDSkillSpec(name, importance, tuple(self.aliases.get(name, [])))
            for importance, names in (("core", self.required_skills.core), ("supporting", self.required_skills.supporting))
            for name in names
        ]

    def preferred_skill_specs(self) -> list[JDSkillSpec]:
        return [JDSkillSpec(name, None, tuple(self.aliases.get(name, []))) for name in self.preferred_skills]


class JDExtractionGenerationSchema(BaseModel):
    """
    Structured-output schema sent to the LLM. Same shape as
    JDExtractionResponse except: no `metadata`, and `aliases` is a list of
    SkillAliasEntry instead of a map - Gemini's Developer API mode rejects
    open-ended dict fields (they compile to JSON Schema
    `additionalProperties`). JDExtractionResponse converts both back.
    """
    required_skills: RequiredSkills = Field(default_factory=RequiredSkills)
    preferred_skills: list[str] = Field(default_factory=list)
    aliases: list[SkillAliasEntry] = Field(default_factory=list)
    domain_capabilities: DomainCapabilities = Field(default_factory=DomainCapabilities)
    soft_skills: list[str] = Field(default_factory=list)
    responsibilities: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    experience: Experience | None = None
    education: Education | None = None
    employment_type: str | None = None
    work_mode: str | None = None
    location: str | None = None
