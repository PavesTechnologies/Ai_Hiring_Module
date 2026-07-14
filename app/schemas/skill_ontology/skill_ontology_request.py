from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field


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


class SkillOntologyUpdateRequest(BaseModel):
    """PATCH body for editing a skill. Only fields present in the request are applied."""

    canonical_name: Optional[str] = Field(default=None, min_length=1)
    aliases: Optional[list[str]] = None
    category: Optional[str] = None
    parent_skill_id: Optional[UUID] = None
    confidence: Optional[Literal["verified", "unverified"]] = None
    source: Optional[Literal["seed", "admin", "auto_extracted"]] = None
    is_active: Optional[bool] = None


class SkillCreateRequest(BaseModel):
    """POST body for creating a new canonical skill."""

    canonical_name: str = Field(..., min_length=1)
    aliases: list[str] = Field(default_factory=list)
    category: Optional[str] = None
    parent_skill_id: Optional[UUID] = None
    confidence: Literal["verified", "unverified"] = "unverified"
    source: Literal[
        "manual entry",
        "seed import",
        "jd extraction",
        "resume extraction",
    ] = "manual entry"
    is_active: bool = True


class SkillStatusUpdateRequest(BaseModel):
    """PATCH body for activating/deactivating a skill (soft delete)."""

    is_active: bool
