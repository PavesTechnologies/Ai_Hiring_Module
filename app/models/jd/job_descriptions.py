import enum
import uuid
from datetime import datetime
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean, DateTime, Enum as SAEnum, ForeignKey, Index,
    Integer, Numeric, String, Text, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class JDSourceFormat(enum.Enum):
    PDF = "PDF"
    DOCX = "DOCX"
    TEXT = "TEXT"


class EmbeddingStatus(enum.Enum):
    PENDING = "PENDING"
    GENERATING = "GENERATING"
    READY = "READY"
    FAILED = "FAILED"


class JobDescription(Base):
    __tablename__ = "job_descriptions"
    __table_args__ = (
        Index(
            "uq_jd_active_lineage_version",
            "lineage_root_id",
            unique=True,
            postgresql_where=text("is_active_version = TRUE"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    parsed_skills: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    required_skills: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    min_experience_years: Mapped[Optional[float]] = mapped_column(Numeric(4, 1), nullable=True)
    education_criteria: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    source_format: Mapped[JDSourceFormat] = mapped_column(SAEnum(JDSourceFormat, name="jd_source_format_enum"), nullable=False)
    file_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active_version: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    lineage_root_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("job_descriptions.id"), nullable=True)
    parent_jd_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("job_descriptions.id"), nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    jurisdiction: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class JDEmbedding(Base):
    __tablename__ = "jd_embeddings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    jd_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("job_descriptions.id"), unique=True, nullable=False)
    embedding: Mapped[list] = mapped_column(Vector(384), nullable=False)
    embedding_model_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("embedding_model_versions.id"), nullable=False)
    input_text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_status: Mapped[EmbeddingStatus] = mapped_column(SAEnum(EmbeddingStatus, name="embedding_status_enum"), nullable=False, default=EmbeddingStatus.PENDING)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


