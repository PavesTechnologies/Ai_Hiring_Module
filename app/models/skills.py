import enum
import uuid
from datetime import datetime
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean, DateTime, Enum as SAEnum, Float, ForeignKey,
    Index, Integer, Numeric, String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class SkillSuggestionStatus(enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class SkillOntology(Base):
    __tablename__ = "skill_ontology"
    __table_args__ = (
        Index("idx_skill_ontology_aliases", "aliases", postgresql_using="gin"),
        Index(
            "idx_skill_ontology_embedding",
            "embedding",
            postgresql_using="ivfflat",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    canonical_name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    aliases = mapped_column(ARRAY(String), nullable=True)
    category: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    parent_skill_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("skill_ontology.id"), nullable=True)
    embedding: Mapped[Optional[list]] = mapped_column(Vector(384), nullable=True)
    confidence: Mapped[str] = mapped_column(Text, nullable=False, default="unverified")
    source: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    embedding_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class UnknownSkill(Base):
    __tablename__ = "unknown_skills"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    raw_text: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    frequency: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    normalized_key: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    # use_alter defers this FK constraint — skill_suggestions is defined after this class
    skill_suggestion_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("skill_suggestions.id", use_alter=True, name="fk_unknown_skill_suggestion_id"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="PENDING")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SkillSuggestion(Base):
    __tablename__ = "skill_suggestions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    raw_skill_text: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_text: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    unknown_skill_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("unknown_skills.id"), nullable=True)
    suggested_canonical_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    suggested_parent_skill_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("skill_ontology.id"), nullable=True)
    status: Mapped[SkillSuggestionStatus] = mapped_column(SAEnum(SkillSuggestionStatus, name="skill_suggestion_status_enum"), nullable=False, default=SkillSuggestionStatus.PENDING)
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(255), ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class JDSkill(Base):
    __tablename__ = "jd_skills"
    __table_args__ = (UniqueConstraint("jd_id", "canonical_skill_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    jd_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("job_descriptions.id"), nullable=False)
    canonical_skill_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("skill_ontology.id"), nullable=False)
    mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Business-set importance for scoring (e.g. HR weighting this skill
    # higher for the deterministic/semantic scoring layers) — never
    # populated by the automated pipeline, which has no business-weight
    # source of its own. Distinct from confidence below.
    weight: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True)
    # How confident the normalization match was (1.0 for exact/alias/case/
    # rule-based, fuzzy_score/100 for RapidFuzz matches) — matches the
    # existing CandidateSkill.confidence column (same table family, resume
    # side), kept separate from `weight` rather than overloading one column.
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Which of the normalization tiers produced this match (EXACT/ALIAS/
    # CASE_INSENSITIVE/RULE_BASED/FUZZY/SEMANTIC) — matches the existing
    # CandidateSkill.match_tier column (same table family, resume side).
    match_tier: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class JDUnknownSkill(Base):
    """
    Traces a specific JD's occurrence of an otherwise-globally-deduped
    UnknownSkill row. UnknownSkill itself stays deduped by raw_text with an
    org-wide frequency counter (one row can originate from many JDs); this
    join table is what makes "which JDs produced this unknown skill"
    queryable without changing that dedup design.
    """

    __tablename__ = "jd_unknown_skills"
    __table_args__ = (UniqueConstraint("jd_id", "unknown_skill_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    jd_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("job_descriptions.id"), nullable=False)
    unknown_skill_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("unknown_skills.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CandidateSkill(Base):
    __tablename__ = "candidate_skills"
    __table_args__ = (
        Index(
            "uq_candidate_skills_resume_canonical",
            "resume_id",
            "canonical_skill_id",
            unique=True,
            postgresql_where=text("canonical_skill_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    candidate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("candidates.id"), nullable=False)
    resume_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("resumes.id"), nullable=False)
    canonical_skill_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("skill_ontology.id"), nullable=True)
    raw_extracted_text: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    match_tier: Mapped[str] = mapped_column(Text, nullable=False)
    scoring_weight: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False, default=1.0)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
