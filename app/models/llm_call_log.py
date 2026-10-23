"""
One row per real request to an LLM provider (main or backup model) - every
caller: parsing, JD extraction, AI evaluation, skill confirmation votes, the
Settings key check, scripts and dry runs. The daily usage (and the v2 daily
cap) is counted from here, per the provider's quota day.
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class LLMCallLog(Base):
    __tablename__ = "llm_call_log"
    __table_args__ = (Index("ix_llm_call_log_created_at", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    # LLMPurpose (EXTRACTION / EVALUATION) the provider was resolved for, when known.
    purpose: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    # What made the call: parsing, ai_evaluation, skill_confirmation, settings_check, ...
    source: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    # app (API / workers) or a script's label (dry_run, comparison, ...).
    context: Mapped[str] = mapped_column(String(60), nullable=False, default="app")
    # ok, or the failure reason (RATE_LIMIT, DAILY_LIMIT, TIMEOUT, ...).
    outcome: Mapped[str] = mapped_column(String(40), nullable=False)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
