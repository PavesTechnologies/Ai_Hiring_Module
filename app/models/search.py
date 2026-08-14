import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class SearchQuery(Base):
    __tablename__ = "search_queries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    queried_by: Mapped[Optional[str]] = mapped_column(String(255), ForeignKey("users.id"), nullable=True)
    campaign_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("hiring_campaigns.id"), nullable=True)
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    query_embedding_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    result_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    top_score: Mapped[Optional[float]] = mapped_column(Numeric(7, 6), nullable=True)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    search_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # M11-E03-S01-T03 — canonical_skill_id UUIDs (as strings) selected for a
    # skill search. The spec's zero_results flag is derivable from
    # result_count == 0, so it is not stored separately.
    canonical_skill_ids: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
