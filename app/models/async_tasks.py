import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime, Enum as SAEnum, ForeignKey, Integer,
    SmallInteger, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class TaskStatus(enum.Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    RETRY = "RETRY"
    DEAD = "DEAD"


class BulkUploadStatus(enum.Enum):
    PENDING = "PENDING"
    EXTRACTING = "EXTRACTING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"


class DocumentType(enum.Enum):
    JD = "JD"
    RESUME = "RESUME"


class ProcessingStage(enum.Enum):
    VALIDATION = "VALIDATION"
    STORAGE = "STORAGE"
    TEXT_EXTRACTION = "TEXT_EXTRACTION"
    TEXT_CLEANING = "TEXT_CLEANING"
    AI_EXTRACTION = "AI_EXTRACTION"
    JSON_VALIDATION = "JSON_VALIDATION"
    SKILL_NORMALIZATION = "SKILL_NORMALIZATION"
    EMBEDDING_GENERATION = "EMBEDDING_GENERATION"
    PERSISTENCE = "PERSISTENCE"


class StageExecutionStatus(enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class CeleryTaskLog(Base):
    __tablename__ = "celery_task_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    task_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resume_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("resumes.id"), nullable=True)
    campaign_candidate_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("campaign_candidates.id"), nullable=True)
    jd_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("job_descriptions.id"), nullable=True)
    status: Mapped[TaskStatus] = mapped_column(SAEnum(TaskStatus, name="task_status_enum"), nullable=False, default=TaskStatus.QUEUED)
    retry_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    worker_hostname: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    input_payload_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    output_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    token_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class DeadLetterQueue(Base):
    __tablename__ = "dead_letter_queue"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    original_task_id: Mapped[str] = mapped_column(String(255), ForeignKey("celery_task_log.task_id"), nullable=False)
    task_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resume_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("resumes.id"), nullable=True)
    campaign_candidate_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("campaign_candidates.id"), nullable=True)
    final_error_message: Mapped[str] = mapped_column(Text, nullable=False)
    full_error_trace: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    input_payload: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    retry_count: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    first_attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    moved_to_dlq_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    replayed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    replayed_by: Mapped[Optional[str]] = mapped_column(String(255), ForeignKey("users.id"), nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class BulkUploadJob(Base):
    __tablename__ = "bulk_upload_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hiring_campaigns.id"), nullable=False)
    uploaded_by: Mapped[str] = mapped_column(String(255), ForeignKey("users.id"), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    total_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    queued_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[BulkUploadStatus] = mapped_column(SAEnum(BulkUploadStatus, name="bulk_upload_status_enum"), nullable=False, default=BulkUploadStatus.PENDING)
    error_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class DocumentProcessingStageExecution(Base):
    """
    Per-stage progress log for an async document-processing pipeline run,
    grouped by `task_id` (the Celery task_id, shared with CeleryTaskLog).
    Document-type-agnostic so a future Resume pipeline reuses it as-is.
    """

    __tablename__ = "document_processing_stage_executions"
    __table_args__ = (UniqueConstraint("task_id", "stage", "attempt_number"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[str] = mapped_column(String(255), nullable=False)
    document_type: Mapped[DocumentType] = mapped_column(SAEnum(DocumentType, name="document_type_enum"), nullable=False)
    document_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    stage: Mapped[ProcessingStage] = mapped_column(SAEnum(ProcessingStage, name="processing_stage_enum"), nullable=False)
    status: Mapped[StageExecutionStatus] = mapped_column(SAEnum(StageExecutionStatus, name="stage_execution_status_enum"), nullable=False)
    attempt_number: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
