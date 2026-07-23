import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, Enum as SAEnum, ForeignKey, Integer,
    Numeric, SmallInteger, String, Text, func,
)
from sqlalchemy.dialects.postgresql import BYTEA, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class FileFormat(enum.Enum):
    PDF = "PDF"
    DOCX = "DOCX"
    PNG = "PNG"
    JPEG = "JPEG"


class ParseStatus(enum.Enum):
    PENDING = "PENDING"
    PARSING = "PARSING"
    PARSED = "PARSED"
    FAILED = "FAILED"


class ParseAttemptStatus(enum.Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"


class Candidate(Base):
    __tablename__ = "candidates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=True)
    full_name_encrypted: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    email_encrypted: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    email_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    phone_encrypted: Mapped[Optional[bytes]] = mapped_column(BYTEA, nullable=True)
    phone_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    encryption_key_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("encryption_keys.id"), nullable=False)
    jurisdiction: Mapped[str] = mapped_column(String(10), nullable=False, default="GLOBAL")
    consent_given: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    consent_timestamp: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    consent_source: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    source_campaign_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("hiring_campaigns.id"), nullable=True)
    erasure_requested_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    erasure_completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    is_pii_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class Resume(Base):
    __tablename__ = "resumes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    candidate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("candidates.id"), nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_format: Mapped[FileFormat] = mapped_column(SAEnum(FileFormat, name="file_format_enum"), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active_version: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    parsed_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    parse_status: Mapped[ParseStatus] = mapped_column(SAEnum(ParseStatus, name="parse_status_enum"), nullable=False, default=ParseStatus.PENDING)
    parse_confidence_score: Mapped[Optional[float]] = mapped_column(Numeric(4, 3), nullable=True)
    parser_version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    page_count: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    parse_duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ocr_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    uploaded_by: Mapped[str] = mapped_column(String(255), ForeignKey("users.id"), nullable=False)
    # Bulk ZIP Upload (M05-E02) — NULL for every individual (M05-E01) upload;
    # set only when this resume was extracted from a bulk_upload_jobs ZIP.
    bulk_upload_job_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("bulk_upload_jobs.id"), nullable=True)
    # Set once, at enqueue time, before the Celery task is ever dispatched —
    # not derived from celery_task_log.resume_id, which is only populated on
    # that task's first success. Stable across retries (a retry reuses the
    # same task_id). Lets a future monitoring API resolve resume_id -> task_id
    # at any point in the resume's lifecycle, not just after it first succeeds.
    task_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class ResumeParseAttempt(Base):
    __tablename__ = "resume_parse_attempts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    resume_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("resumes.id"), nullable=False)
    attempt_number: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    parser_used: Mapped[str] = mapped_column(String(100), nullable=False)
    parser_version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    ocr_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[ParseAttemptStatus] = mapped_column(SAEnum(ParseAttemptStatus, name="parse_attempt_status_enum"), nullable=False)
    error_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    error_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    confidence_score: Mapped[Optional[float]] = mapped_column(Numeric(4, 3), nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
