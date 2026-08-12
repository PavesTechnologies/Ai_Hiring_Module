import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class UserSavedView(Base):
    """
    M11-E03-S03 — a named filter/sort configuration for one user on one
    campaign.

    Stored server-side rather than in browser local storage so views follow the
    user across devices and MAX_SAVED_VIEWS_PER_USER is actually enforceable
    (a client-side limit is advisory at best).
    """

    __tablename__ = "user_saved_views"
    __table_args__ = (
        # a user can't have two views with the same name on one campaign
        UniqueConstraint("user_id", "campaign_id", "name", name="uq_saved_view_user_campaign_name"),
        # the only read pattern: every view for this user on this campaign
        Index("ix_user_saved_views_user_campaign", "user_id", "campaign_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(String(255), ForeignKey("users.id"), nullable=False)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hiring_campaigns.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Whole filter + sort state as sent by the UI. Deliberately schemaless:
    # E03's filter set is still growing, and a rigid column per filter would
    # need a migration every time one is added.
    filters: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    last_applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
