from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field
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


class BulkImportFailureResponse(BaseModel):
    """S07-T02: one row that could not be processed, and why."""

    row: int
    reason: str


class BulkImportResponse(BaseModel):
    inserted: int
    updated: int = 0
    skipped: int
    failed: int
    failures: list[BulkImportFailureResponse] = []
    import_id: Optional[UUID] = Field(
        default=None,
        description="Set only when failed > 0 — pass to GET /import/errors/{import_id} to download the error report.",
    )


class BulkImportValidationErrorResponse(BaseModel):
    """S07-T01: one row-level validation finding — may or may not block execution (see BulkImportValidationResponse)."""

    row: Optional[int] = None
    column: Optional[str] = None
    message: str


class BulkImportValidationResponse(BaseModel):
    """
    S07-T01: dry-run only — never writes to the database. success is False
    only for a file-level failure (unreadable file, missing required
    columns) that prevented row-by-row validation from running at all; a
    file with per-row issues but valid_rows > 0 still reports success=True
    so the caller can see the full picture before deciding whether to
    proceed to the actual POST /import.
    """

    success: bool
    total_rows: int = 0
    valid_rows: int = 0
    invalid_rows: int = 0
    validation_errors: list[BulkImportValidationErrorResponse] = []


class SkillHierarchyNodeResponse(BaseModel):
    """One node of the Skill Hierarchy tree (S05-T02) — lazily loaded, one level at a time."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    canonical_name: str
    confidence: str
    is_active: bool
    has_children: bool


class SkillDeactivationImpactResponse(BaseModel):
    """
    S06-T01: pre-deactivation preview — never mutates anything. can_deactivate
    is False only when there's nothing to do (skill already inactive);
    otherwise usage/children are informational, surfaced so the caller can
    confirm (and, if there are children, choose child_handling) before the
    actual PATCH .../status call.
    """

    can_deactivate: bool
    candidate_usage: int
    jd_usage: int
    warning: Optional[str] = None
    children: list[str] = []
