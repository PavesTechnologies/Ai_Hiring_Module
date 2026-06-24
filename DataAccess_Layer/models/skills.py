import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Enum as SAEnum, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from DataAccess_Layer.utils.db_connection import Base


class SkillSuggestionStatus(enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class Skill(Base):
    __tablename__ = "skills"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    canonical_name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    aliases: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    parent_skill_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("skills.id"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SkillSuggestion(Base):
    __tablename__ = "skill_suggestions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    raw_skill_text: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_text: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    suggested_canonical_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    status: Mapped[SkillSuggestionStatus] = mapped_column(SAEnum(SkillSuggestionStatus, name="skill_suggestion_status_enum"), nullable=False, default=SkillSuggestionStatus.PENDING)
    reviewed_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
