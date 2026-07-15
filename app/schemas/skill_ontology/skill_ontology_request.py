from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

CONFIDENCE_VALUES = ("verified", "unverified")
SOURCE_VALUES = ("seed", "manual entry", "jd extraction", "resume extraction")


def _normalize_choice(value: Optional[str], *, allowed: tuple[str, ...], label: str) -> Optional[str]:
    """Trims/lowercases a case-insensitive choice field; rejects anything outside `allowed`."""
    if value is None:
        return value

    normalized = value.strip().lower()
    if normalized not in allowed:
        raise ValueError(f"{label} must be one of: {', '.join(allowed)}")

    return normalized


class ConfidenceSourceNormalizationMixin:
    """
    Shared before-validators for the `confidence`/`source` choice fields.

    Mix into any request schema that declares those fields so casing like
    "VERIFIED" or "Manual Entry" is normalized before Pydantic's own Literal
    check runs, instead of every model re-declaring the same validator.
    """

    @field_validator("confidence", mode="before")
    @classmethod
    def _normalize_confidence(cls, value):
        return _normalize_choice(value, allowed=CONFIDENCE_VALUES, label="Confidence")

    @field_validator("source", mode="before")
    @classmethod
    def _normalize_source(cls, value):
        return _normalize_choice(value, allowed=SOURCE_VALUES, label="Source")


class SkillOntologyFilterRequest(BaseModel):
    page: int = Field(default=1, ge=1)

    page_size: int = Field(default=20, ge=1, le=100)

    search: str | None = None

    category: str | None = None

    confidence: Literal[
        "verified",
        "unverified",
    ] | None = None

    source: Literal[
        "seed",
        "admin",
        "auto_extracted",
    ] | None = None

    include_inactive: bool = False

    sort_by: str = "occurrence_count"

    sort_order: Literal[
        "asc",
        "desc",
    ] = "desc"


class SkillOntologyUpdateRequest(BaseModel, ConfidenceSourceNormalizationMixin):
    """PATCH body for editing a skill. Only fields present in the request are applied."""

    canonical_name: Optional[str] = Field(default=None, min_length=1)
    aliases: Optional[list[str]] = None
    remove_aliases: Optional[list[str]] = None
    confirm_alias_removal: bool = False
    category: Optional[str] = None
    parent_skill_id: Optional[UUID] = None
    confidence: Optional[Literal["verified", "unverified"]] = None
    source: Optional[Literal["seed", "manual entry", "jd extraction", "resume extraction"]] = None
    is_active: Optional[bool] = None


class SkillCreateRequest(BaseModel, ConfidenceSourceNormalizationMixin):
    """POST body for creating a new canonical skill."""

    canonical_name: str = Field(..., min_length=1)
    aliases: list[str] = Field(default_factory=list)
    category: Optional[str] = None
    parent_skill_id: Optional[UUID] = None
    confidence: Literal["verified", "unverified"] = "unverified"
    source: Literal[
        "seed",
        "manual entry",
        "jd extraction",
        "resume extraction",
    ] = "manual entry"
    is_active: bool = True


class SkillStatusUpdateRequest(BaseModel):
    """
    PATCH body for activating/deactivating a skill (soft delete). If the
    skill being deactivated has children, child_handling is required:
    'PROMOTE' (children move up to this skill's own parent), 'ROOT'
    (children become root skills), or 'CANCEL' (abort — nothing changes,
    including is_active). Ignored when reactivating or when the skill has
    no children.
    """

    is_active: bool
    child_handling: Optional[Literal["PROMOTE", "ROOT", "CANCEL"]] = None
