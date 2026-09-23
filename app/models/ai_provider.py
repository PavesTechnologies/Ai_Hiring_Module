import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import BYTEA, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class AIProviderConfig(Base):
    """
    Admin-selected LLM provider used by every AI step (JD/resume extraction,
    AI evaluation). At most one row is active (partial unique index below);
    saving a new config updates that row in place, and history lives in the
    audit log. `provider` is a plain varchar (see LLMProviderName), same
    reasoning as UserOAuthToken.provider. The API key is encrypted at rest
    with the same BYTEA + encryption_key_id convention as oauth tokens;
    api_key_last4 exists only so the UI can show which key is saved.
    """
    __tablename__ = "ai_provider_config"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    api_key_encrypted: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    encryption_key_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("encryption_keys.id"), nullable=False,
    )
    api_key_last4: Mapped[str] = mapped_column(String(8), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_by: Mapped[Optional[str]] = mapped_column(String(255), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )

    __table_args__ = (
        Index(
            "uq_ai_provider_config_single_active",
            "is_active",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )
