import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import BYTEA, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class AIProviderConfig(Base):
    """
    AI providers registered in Settings - at most one row per provider
    (unique constraint below). Exactly one row is active (partial unique
    index below): that's the provider every AI step (JD/resume extraction,
    AI evaluation) uses. History of changes lives in the audit log.
    `provider` is a plain varchar (see LLMProviderName), same reasoning as
    UserOAuthToken.provider. Which model each AI step uses lives in
    `models` (AIProviderModel); `model_name` mirrors the extraction model
    for display and for callers that predate per-purpose models. The API key is encrypted at rest
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

    models: Mapped[list["AIProviderModel"]] = relationship(
        back_populates="provider_config",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="AIProviderModel.usage",
    )

    __table_args__ = (
        UniqueConstraint("provider", name="uq_ai_provider_config_provider"),
        Index(
            "uq_ai_provider_config_single_active",
            "is_active",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )


class AIProviderModel(Base):
    """
    One model assignment on a registered provider: which model runs a
    purpose (EXTRACTION = JD + resume parsing, EVALUATION = AI evaluation,
    BOTH = either), plus an optional backup model on the same provider/key.
    Rules (one model per purpose, no model listed twice, backup differs
    from its main model, both purposes covered) are validated in
    app.services.llm.model_selection; the constraints below are the
    database backstop for the first three.
    """
    __tablename__ = "ai_provider_model"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider_config_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_provider_config.id", ondelete="CASCADE"), nullable=False,
    )
    usage: Mapped[str] = mapped_column(String(20), nullable=False)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    fallback_model_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    provider_config: Mapped["AIProviderConfig"] = relationship(back_populates="models")

    __table_args__ = (
        UniqueConstraint("provider_config_id", "usage", name="uq_ai_provider_model_usage"),
        UniqueConstraint("provider_config_id", "model_name", name="uq_ai_provider_model_model"),
        CheckConstraint("usage IN ('EXTRACTION', 'EVALUATION', 'BOTH')", name="ck_ai_provider_model_usage"),
        CheckConstraint(
            "fallback_model_name IS NULL OR fallback_model_name <> model_name",
            name="ck_ai_provider_model_fallback_differs",
        ),
    )
