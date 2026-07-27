from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.skill_ontology.skill_ontology_request import ConfidenceSourceNormalizationMixin


class UnknownSkillItem(BaseModel):
    id: UUID
    raw_text: str
    normalized_key: str | None
    frequency: int
    first_seen: datetime
    last_seen: datetime
    status: str


class MapUnknownSkillRequest(BaseModel):
    target_skill_id: UUID
    save_as_alias: bool = False


class PromoteUnknownSkillRequest(BaseModel):
    category: str | None = None


class CreateCanonicalSkillFromUnknownRequest(BaseModel, ConfidenceSourceNormalizationMixin):
    """POST body for promoting an UnknownSkill into a fully-specified canonical skill."""

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


class CreateCanonicalSkillFromUnknownResponse(BaseModel):
    id: UUID
    canonical_name: str
    aliases: list[str]
    category: str | None
    parent_skill_id: UUID | None
    confidence: str
    source: str | None
    is_active: bool
    jd_skills_migrated: int
    candidate_skills_migrated: int


class BulkUnknownSkillIdsRequest(BaseModel):
    """Shared POST body for the bulk-approve/bulk-delete unknown-skill endpoints."""

    unknown_skill_ids: list[UUID] = Field(..., min_length=1)


class BulkUnknownSkillResultItem(BaseModel):
    """
    One id's outcome from a bulk approve/delete run. Fields not relevant to
    the action that produced this item (e.g. canonical_name on a delete
    result) are left None rather than split into two separate response
    shapes.
    """

    unknown_skill_id: UUID
    success: bool
    message: str
    canonical_skill_id: Optional[UUID] = None
    canonical_name: Optional[str] = None
    jd_skills_migrated: Optional[int] = None
    candidate_skills_migrated: Optional[int] = None
    jd_unknown_skills_deleted: Optional[int] = None
    candidate_skills_deleted: Optional[int] = None


class BulkUnknownSkillActionResponse(BaseModel):
    results: list[BulkUnknownSkillResultItem]
    succeeded: int
    failed: int


class RemapJDSkillRequest(BaseModel):
    new_canonical_skill_id: UUID


class UnknownSkillActionResponse(BaseModel):
    id: UUID
    raw_text: str
    status: str


class UnknownSkillDeleteResponse(BaseModel):
    id: UUID
    raw_text: str
    jd_unknown_skills_deleted: int
    candidate_skills_deleted: int


class PromotedSkillResponse(BaseModel):
    id: UUID
    canonical_name: str


class JDSkillRemapResponse(BaseModel):
    id: UUID
    jd_id: UUID
    canonical_skill_id: UUID
    match_tier: str


class JDSkillItem(BaseModel):
    id: UUID
    jd_id: UUID
    canonical_skill_id: UUID
    canonical_name: str
    mandatory: bool
    weight: float | None
    confidence: float | None
    match_tier: str
    verification_status: str
    created_at: datetime


class JDUnknownSkillItem(BaseModel):
    id: UUID
    jd_id: UUID
    unknown_skill_id: UUID
    raw_text: str
    mandatory: bool | None
    status: str
    created_at: datetime


class UnknownSkillJDItem(BaseModel):
    id: UUID
    jd_id: UUID
    job_id: str
    title: str
    version_number: int
    is_active_version: bool
    mandatory: bool | None
    status: str
    created_at: datetime


class UnknownSkillCandidateItem(BaseModel):
    id: UUID
    candidate_id: UUID
    resume_id: UUID
    candidate_name: str | None
    raw_extracted_text: str
    confidence: float | None
    match_tier: str
    created_at: datetime
