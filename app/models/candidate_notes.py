import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class CandidateNote(Base):
    """
    M11-E04-S01 — free-text recruiter notes on a candidate within a campaign.

    A dedicated table rather than a column on campaign_candidates: notes are
    many-per-candidate, individually authored, editable and deletable, and the
    story wants a count badge. None of that survives being packed into a text
    field, and decision_reason is already spoken for by the decision model.

    Scoped to campaign_candidate_id, not candidate_id — a note about someone's
    fit is a note about their fit *for this role*, and leaking it across
    campaigns would expose one hiring team's commentary to another.
    """

    __tablename__ = "candidate_notes"
    __table_args__ = (
        # Backs both the per-candidate note list and the count badge, and keeps
        # soft-deleted rows out of the index scan for the common case.
        Index("ix_candidate_notes_cc_id_created_at", "campaign_candidate_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign_candidates.id"), nullable=False,
    )
    note_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    # Set on edit so the UI can show "edited" without a second table.
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Soft delete: a note that influenced a hiring decision must remain
    # auditable after a recruiter removes it from the visible list.
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[Optional[str]] = mapped_column(
        String(255), ForeignKey("users.id"), nullable=True,
    )
