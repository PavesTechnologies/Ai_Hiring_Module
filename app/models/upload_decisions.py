"""
Pending "keep existing / use new version" decisions for bulk-uploaded resumes.

A bulk file whose candidate (same email) is already in the campaign with a
different file is held here instead of becoming their active resume: the
stored file is kept, nothing else changes, and the uploader (or an HR admin)
decides. Single uploads decide before anything is written, so they never
create a row. Pending rows expire after DECISION_TTL_DAYS - the existing
resume is kept and the new file discarded.
"""
import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, SmallInteger, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base

DECISION_TTL_DAYS = 30


class UploadDecisionStatus(str, enum.Enum):
    PENDING = "PENDING"
    KEPT_EXISTING = "KEPT_EXISTING"
    NEW_VERSION_USED = "NEW_VERSION_USED"
    EXPIRED_KEPT_EXISTING = "EXPIRED_KEPT_EXISTING"


class UploadDecisionChoice(str, enum.Enum):
    KEEP_EXISTING = "keep_existing"
    USE_NEW_VERSION = "use_new_version"


class UploadDecision(Base):
    __tablename__ = "upload_decisions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'KEPT_EXISTING', 'NEW_VERSION_USED', 'EXPIRED_KEPT_EXISTING')",
            name="ck_upload_decisions_status",
        ),
        Index("ix_upload_decisions_campaign_status", "campaign_id", "status"),
        Index("ix_upload_decisions_bulk_upload_job_id", "bulk_upload_job_id"),
        Index("ix_upload_decisions_status_expires_at", "status", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hiring_campaigns.id"), nullable=False)
    # SET NULL so deleting a campaign candidate / erasing a candidate is never
    # blocked by a decision row; a decision whose candidate is gone can only
    # be kept-existing.
    campaign_candidate_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign_candidates.id", ondelete="SET NULL"), nullable=True,
    )
    candidate_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("candidates.id", ondelete="SET NULL"), nullable=True,
    )
    # The campaign candidate's resume when the file was held (shown as "existing").
    existing_resume_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resumes.id", ondelete="SET NULL"), nullable=True,
    )
    bulk_upload_job_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bulk_upload_jobs.id"), nullable=True,
    )
    bulk_upload_job_file_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bulk_upload_job_files.id"), nullable=True,
    )
    # The held file: everything needed to create a Resume from it later.
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    original_filename: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    file_format: Mapped[str] = mapped_column(String(10), nullable=False)
    file_size_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    page_count: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    uploaded_by: Mapped[str] = mapped_column(String(255), ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default=UploadDecisionStatus.PENDING.value,
                                        server_default=UploadDecisionStatus.PENDING.value)
    decided_by: Mapped[Optional[str]] = mapped_column(String(255), ForeignKey("users.id"), nullable=True)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set when "use new version" created it.
    new_resume_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resumes.id", ondelete="SET NULL"), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
