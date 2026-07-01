import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.models.audit import ActionType, EntityType
from sqlalchemy import Enum as SAEnum

from app.db.database import Base


class CandidateConsent(Base):
    __tablename__ = "candidate_consent"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    candidate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("candidates.id"), nullable=False)
    consent_given: Mapped[bool] = mapped_column(Boolean, nullable=False)
    consent_version: Mapped[str] = mapped_column(String(50), nullable=False, default="1.0")
    jurisdiction: Mapped[str] = mapped_column(String(10), nullable=False)
    consent_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consent_source: Mapped[str] = mapped_column(String(100), nullable=False)
    ip_address: Mapped[Optional[str]] = mapped_column(INET, nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revocation_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    actor_role: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    action_type: Mapped[ActionType] = mapped_column(SAEnum(ActionType, name="audit_action_type_enum"), nullable=False)
    entity_type: Mapped[EntityType] = mapped_column(SAEnum(EntityType, name="audit_entity_type_enum"), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    campaign_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("hiring_campaigns.id"), nullable=True)
    jurisdiction: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    detail: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(INET, nullable=True)
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    request_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class RetentionPolicy(Base):
    __tablename__ = "retention_policies"
    __table_args__ = (UniqueConstraint("entity_type", "jurisdiction"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False)
    jurisdiction: Mapped[str] = mapped_column(String(10), nullable=False, default="GLOBAL")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_enforced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
