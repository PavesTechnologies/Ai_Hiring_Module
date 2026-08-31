import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import BYTEA, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class UserOAuthToken(Base):
    """
    M12 - Microsoft Teams calendar integration. One row per (user, provider)
    - `provider` is a plain varchar, not an enum, since Google Meet is a
    likely future second provider and this avoids a migration just to add
    a value. access_token/refresh_token are encrypted at rest (BYTEA, one
    shared encryption_key_id per row) matching candidates.py's exact
    convention for full_name_encrypted/email_encrypted/phone_encrypted -
    a live refresh token is a standing credential, at least as sensitive
    as the PII this codebase already encrypts.
    """
    __tablename__ = "user_oauth_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(String(255), ForeignKey("users.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    access_token_encrypted: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    refresh_token_encrypted: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    encryption_key_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("encryption_keys.id"), nullable=False,
    )
    token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scopes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_user_oauth_tokens_user_id_provider"),
    )
