"""
A person moved a candidate against the system's screening outcome (Phase 4, recruiter overrides):
a PASS rejected or put on hold, a REJECT brought back, or a HOLD resolved. One row per such move,
with the result it contradicted and a reason code - never free text. See
app/services/campaign/outcome_overrides.py for what counts.
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base
from app.models.pipeline import PipelineStage


class CandidateOutcomeOverride(Base):
    __tablename__ = "candidate_outcome_overrides"
    __table_args__ = (
        Index("idx_outcome_overrides_campaign_created", "campaign_id", "created_at"),
        Index("idx_outcome_overrides_campaign_candidate", "campaign_candidate_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign_candidates.id"), nullable=False)
    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hiring_campaigns.id"), nullable=False)
    # The result the move contradicted (the latest one acted on), and what it said.
    scoring_result_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("candidate_scoring_results.id"), nullable=True)
    scoring_version: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    scoring_config_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    system_outcome: Mapped[str] = mapped_column(String(20), nullable=False)        # PASS / HOLD / REJECT
    kind: Mapped[str] = mapped_column(String(20), nullable=False)                  # OVERRIDE / REVIEW_DECISION
    direction: Mapped[str] = mapped_column(String(10), nullable=False)             # UP / DOWN
    from_stage: Mapped[PipelineStage] = mapped_column(SAEnum(PipelineStage, name="pipeline_stage_enum",
                                                             create_type=False), nullable=False)
    to_stage: Mapped[PipelineStage] = mapped_column(SAEnum(PipelineStage, name="pipeline_stage_enum",
                                                           create_type=False), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(40), nullable=False)
    # From the contradicted result, for the patterns report (skill names and check names only).
    failed_checks: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    missing_core_skills: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    profile_template: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    actor_id: Mapped[Optional[str]] = mapped_column(String(255), ForeignKey("users.id"), nullable=True)
    actor_role: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
