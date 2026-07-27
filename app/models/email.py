import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, Enum as SAEnum, ForeignKey, Index, String, Text, func, text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class EmailTriggerEvent(enum.Enum):
    # Only value needed for M07-E03 S02 - additional trigger events (e.g.
    # interview scheduled) are a different epic and intentionally not
    # invented here.
    CANDIDATE_REJECTED = "CANDIDATE_REJECTED"


class EmailNotificationStatus(enum.Enum):
    QUEUED = "QUEUED"
    SENT = "SENT"
    FAILED = "FAILED"


class EmailTemplate(Base):
    __tablename__ = "email_templates"
    __table_args__ = (
        # Same "one active row per key" pattern already used for
        # JobDescription.uq_jd_active_lineage_version - a partial unique
        # index, not a plain UniqueConstraint, since multiple INACTIVE
        # templates for the same trigger_event (draft/superseded versions)
        # must still be allowed.
        Index(
            "uq_email_templates_active_trigger_event",
            "trigger_event",
            unique=True,
            postgresql_where=text("is_active = true"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trigger_event: Mapped[EmailTriggerEvent] = mapped_column(
        SAEnum(EmailTriggerEvent, name="email_trigger_event_enum"), nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    body_template: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class EmailNotification(Base):
    __tablename__ = "email_notifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    candidate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("candidates.id"), nullable=False)
    # Optional: which rejection triggered this, for traceability - not in
    # the ticket's explicit field list but nullable so it never blocks a
    # future trigger_event that isn't campaign_candidate-scoped.
    campaign_candidate_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign_candidates.id"), nullable=True,
    )
    trigger_event: Mapped[EmailTriggerEvent] = mapped_column(
        SAEnum(EmailTriggerEvent, name="email_trigger_event_enum"), nullable=False,
    )
    template_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("email_templates.id"), nullable=False)
    status: Mapped[EmailNotificationStatus] = mapped_column(
        SAEnum(EmailNotificationStatus, name="email_notification_status_enum"),
        nullable=False, default=EmailNotificationStatus.QUEUED,
    )
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # T03: populated only on the terminal FAILED path.
    error_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
