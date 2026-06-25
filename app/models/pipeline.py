import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, Enum as SAEnum,
    ForeignKey, Numeric, SmallInteger, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base

_USERS_FK = "users.id"


class PipelineStage(enum.Enum):
    UPLOADED = "UPLOADED"
    SCREENING = "SCREENING"
    SHORTLISTED = "SHORTLISTED"
    HOLD = "HOLD"
    HM_REVIEW = "HM_REVIEW"
    INTERVIEW = "INTERVIEW"
    SELECTED = "SELECTED"
    REJECTED = "REJECTED"
    FRAUD_REVIEW = "FRAUD_REVIEW"


class AIEvaluationStatus(enum.Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class AIRecommendation(enum.Enum):
    SHORTLIST = "SHORTLIST"
    HOLD = "HOLD"
    REJECT = "REJECT"


class RejectionLayer(enum.Enum):
    DETERMINISTIC = "DETERMINISTIC"
    SEMANTIC = "SEMANTIC"
    AI = "AI"
    MANUAL = "MANUAL"
    FRAUD = "FRAUD"


class TransitionSource(enum.Enum):
    SYSTEM = "SYSTEM"
    MANUAL = "MANUAL"
    TRIGGER = "TRIGGER"
    OVERRIDE = "OVERRIDE"


class CampaignCandidate(Base):
    __tablename__ = "campaign_candidates"
    __table_args__ = (
        UniqueConstraint("campaign_id", "candidate_id", "resume_id"),
        CheckConstraint(
            "composite_score IS NULL OR (composite_score >= 0 AND composite_score <= 100)",
            name="chk_composite_score_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hiring_campaigns.id"), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("candidates.id"), nullable=False)
    resume_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("resumes.id"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    pipeline_stage: Mapped[PipelineStage] = mapped_column(SAEnum(PipelineStage, name="pipeline_stage_enum"), nullable=False, default=PipelineStage.UPLOADED)
    screened_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    deterministic_score: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True)
    deterministic_passed: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    semantic_score: Mapped[Optional[float]] = mapped_column(Numeric(7, 6), nullable=True)
    ai_ats_score: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True)
    ai_confidence: Mapped[Optional[float]] = mapped_column(Numeric(5, 4), nullable=True)
    effective_ai_score: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True)
    ai_recommendation: Mapped[Optional[AIRecommendation]] = mapped_column(SAEnum(AIRecommendation, name="ai_recommendation_enum"), nullable=True)
    ai_strengths: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    ai_weaknesses: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    ai_evaluation_status: Mapped[AIEvaluationStatus] = mapped_column(SAEnum(AIEvaluationStatus, name="ai_evaluation_status_enum"), nullable=False, default=AIEvaluationStatus.PENDING)
    ai_retry_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    prompt_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("prompt_versions.id"), nullable=True)
    composite_score: Mapped[Optional[float]] = mapped_column(Numeric(6, 3), nullable=True)
    fraud_flags: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    is_fraud_flagged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rejection_layer: Mapped[Optional[RejectionLayer]] = mapped_column(SAEnum(RejectionLayer, name="rejection_layer_enum"), nullable=True)
    hr_override: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    hr_override_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey(_USERS_FK), nullable=True)
    hr_override_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    hr_override_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    recruiter_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class AllowedTransition(Base):
    __tablename__ = "allowed_transitions"
    __table_args__ = (UniqueConstraint("from_stage", "to_stage"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    from_stage: Mapped[PipelineStage] = mapped_column(SAEnum(PipelineStage, name="pipeline_stage_enum"), nullable=False)
    to_stage: Mapped[PipelineStage] = mapped_column(SAEnum(PipelineStage, name="pipeline_stage_enum"), nullable=False)
    allowed_roles = mapped_column(ARRAY(String), nullable=False)
    requires_reason: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class CampaignCandidateStageHistory(Base):
    __tablename__ = "campaign_candidate_stage_history"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_candidate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("campaign_candidates.id"), nullable=False)
    from_stage: Mapped[Optional[PipelineStage]] = mapped_column(SAEnum(PipelineStage, name="pipeline_stage_enum"), nullable=True)
    to_stage: Mapped[PipelineStage] = mapped_column(SAEnum(PipelineStage, name="pipeline_stage_enum"), nullable=False)
    changed_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey(_USERS_FK), nullable=True)
    change_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    transition_source: Mapped[TransitionSource] = mapped_column(SAEnum(TransitionSource, name="transition_source_enum"), nullable=False, default=TransitionSource.SYSTEM)
    scores_snapshot: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CandidateRejection(Base):
    __tablename__ = "candidate_rejections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_candidate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("campaign_candidates.id"), nullable=False)
    rejection_layer: Mapped[RejectionLayer] = mapped_column(SAEnum(RejectionLayer, name="rejection_layer_enum"), nullable=False)
    rejection_reason: Mapped[str] = mapped_column(Text, nullable=False)
    rejection_detail: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    rejected_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey(_USERS_FK), nullable=True)
    rejected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
