from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict


class SkillOntologyItemResponse(BaseModel):

    model_config = ConfigDict(
        from_attributes=True,
    )

    id: UUID

    canonical_name: str

    aliases: list[str]

    category: str

    confidence: str

    source: str

    is_active: bool

    occurrence_count: int

    last_seen_at: datetime | None


class SkillOntologySummaryResponse(BaseModel):
    total_skills: int
    verified_skills: int
    unverified_skills: int
    active_skills: int
    inactive_skills: int
    categories: int


class SkillCategoryResponse(BaseModel):
    category: str
    count: int


class SkillOntologyListResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    canonical_name: str
    aliases: list[str]
    category: Optional[str]
    parent_skill_name: Optional[str]
    confidence: str
    source: Optional[str]
    occurrence_count: int
    is_active: bool
    created_at: datetime


class SkillOntologyPageResponse(BaseModel):
    items: list[SkillOntologyListResponse]
    page: int
    page_size: int
    total: int


class SkillOntologyChildResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    canonical_name: str


class SkillOntologyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    canonical_name: str
    aliases: list[str]
    category: Optional[str]
    parent_skill_name: Optional[str]
    confidence: str
    source: Optional[str]
    occurrence_count: int
    is_active: bool
    created_at: datetime
    children: list[SkillOntologyChildResponse]
    embedding_status: str


class SimilarSkillResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    canonical_name: str
    category: Optional[str]
    similarity_score: int


class SkillCreateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    canonical_name: str
    aliases: list[str]
    category: Optional[str]
    parent_skill_id: Optional[UUID]
    confidence: str
    source: Optional[str]
    is_active: bool
    occurrence_count: int
    created_at: datetime
    skill_created: bool = True
    similar_skills: list[SimilarSkillResponse] = []


class ParentSkillResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    canonical_name: str


class BulkImportResponse(BaseModel):
    inserted: int
    skipped: int
    failed: int
