import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, Enum as SAEnum,
    ForeignKey, Index, Numeric, SmallInteger, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

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
    # M07-E03 S01 T03: set when a candidate is rejected at the DETERMINISTIC
    # layer and any QUEUED AI_EVALUATE task for them is cancelled - distinct
    # from PENDING (never queued yet) and FAILED (queued, ran, errored).
    SKIPPED = "SKIPPED"


class AIRecommendation(enum.Enum):
    SHORTLIST = "SHORTLIST"
    HOLD = "HOLD"
    REJECT = "REJECT"


class DecisionType(enum.Enum):
    REJECTED = "REJECTED"
    SHORTLISTED = "SHORTLISTED"
    SELECTED = "SELECTED"
    HOLD = "HOLD"
    FRAUD_REVIEW = "FRAUD_REVIEW"
    RESET = "RESET"


class DecisionSource(enum.Enum):
    DETERMINISTIC = "DETERMINISTIC"
    SEMANTIC = "SEMANTIC"
    AI = "AI"
    RECRUITER = "RECRUITER"
    HIRING_MANAGER = "HIRING_MANAGER"
    HR_ADMIN = "HR_ADMIN"
    SYSTEM = "SYSTEM"


class TransitionSource(enum.Enum):
    SYSTEM = "SYSTEM"
    MANUAL = "MANUAL"
    TRIGGER = "TRIGGER"
    OVERRIDE = "OVERRIDE"


class CompositeScoreTriggerSource(enum.Enum):
    """
    M10-E01: what caused a composite_score (re)calculation. Three valid
    triggers - AI Evaluation completing with a non-REJECT recommendation, a
    candidate being rejected at any of the 3 automated screening layers
    (DETERMINISTIC/SEMANTIC/AI), and a campaign's scoring weights changing.
    Never resume upload/parsing/reprocessing/reset or an HR override - an
    HR override only restarts the remaining scoring pipeline (deterministic
    re-pass -> semantic -> AI evaluation); it is that pipeline's own eventual
    success/rejection which (re)triggers Composite Score, not the override
    itself.
    """
    AI_EVALUATION = "AI_EVALUATION"
    REJECTION = "REJECTION"
    CAMPAIGN_WEIGHT_CHANGE = "CAMPAIGN_WEIGHT_CHANGE"


class CampaignCandidate(Base):
    __tablename__ = "campaign_candidates"
    __table_args__ = (
        UniqueConstraint("campaign_id", "candidate_id", "resume_id"),
        CheckConstraint(
            "composite_score IS NULL OR (composite_score >= 0 AND composite_score <= 100)",
            name="chk_composite_score_range",
        ),
        # M10-E03 Phase 1: backs the ranked candidate list's default query -
        # WHERE campaign_id = X ORDER BY composite_score [ASC|DESC] NULLS
        # LAST - avoiding a full per-campaign scan + in-memory sort.
        Index("ix_campaign_candidates_campaign_id_composite_score", "campaign_id", "composite_score"),
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
    deterministic_breakdown: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    semantic_score: Mapped[Optional[float]] = mapped_column(Numeric(5, 4), nullable=True)
    semantic_passed: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # Task 539: set alongside semantic_score/updated_at on every successful
    # computation - when the currently-stored semantic_score was computed,
    # distinct from updated_at (which also moves on unrelated edits).
    semantic_score_computed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # M08-E02: semantic-layer analog of deterministic_breakdown - overall_similarity/
    # semantic_passed/semantic_threshold/matching_skills/missing_skills/
    # matched_keywords/semantic_explanation, written by SemanticScoringService.
    semantic_breakdown: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    composite_score: Mapped[Optional[float]] = mapped_column(Numeric(6, 3), nullable=True)
    # M10-E01: when composite_score was last (re)computed - None until the
    # first successful calculation, overwritten (never appended) on every
    # subsequent one. The immutable per-calculation trail lives in
    # candidate_composite_score_history instead.
    composite_score_computed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    fraud_flags: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    is_fraud_flagged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Unified decision model: replaces CandidateRejection, rejection_reason/
    # rejection_layer, and hr_override* alike - every "why is this candidate
    # in this state" fact (AI/deterministic/semantic rejection, recruiter
    # shortlist/reject, HM selection, manual hold, fraud review, HR override
    # reset) is one decision, regardless of who/what made it.
    decision_type: Mapped[Optional[DecisionType]] = mapped_column(SAEnum(DecisionType, name="decision_type_enum"), nullable=True)
    decision_source: Mapped[Optional[DecisionSource]] = mapped_column(SAEnum(DecisionSource, name="decision_source_enum"), nullable=True)
    decision_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    decision_details: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    decision_by_user_id: Mapped[Optional[str]] = mapped_column(String(255), ForeignKey(_USERS_FK), nullable=True)
    decision_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    ai_evaluation: Mapped[Optional["CampaignCandidateAIEvaluation"]] = relationship(
        back_populates="campaign_candidate", uselist=False, cascade="all, delete-orphan",
    )


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
    changed_by: Mapped[Optional[str]] = mapped_column(String(255), ForeignKey(_USERS_FK), nullable=True)
    change_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    transition_source: Mapped[TransitionSource] = mapped_column(SAEnum(TransitionSource, name="transition_source_enum"), nullable=False, default=TransitionSource.SYSTEM)
    scores_snapshot: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class CampaignCandidateAIEvaluation(Base):
    """
    1:1 with CampaignCandidate - every AI-provider-specific field, split out
    of the core workflow row so AI evaluation output (and its own retry/
    status bookkeeping) doesn't live mixed in with pipeline state.
    AIEvaluationService is the only writer of the ai_* columns here.
    """
    __tablename__ = "campaign_candidate_ai_evaluations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign_candidates.id"), unique=True, nullable=False,
    )
    ai_evaluation_status: Mapped[AIEvaluationStatus] = mapped_column(SAEnum(AIEvaluationStatus, name="ai_evaluation_status_enum"), nullable=False, default=AIEvaluationStatus.PENDING)
    ai_retry_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    ai_ats_score: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True)
    effective_ai_score: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True)
    ai_confidence: Mapped[Optional[float]] = mapped_column(Numeric(5, 4), nullable=True)
    ai_recommendation: Mapped[Optional[AIRecommendation]] = mapped_column(SAEnum(AIRecommendation, name="ai_recommendation_enum"), nullable=True)
    ai_strengths: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    ai_weaknesses: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    ai_response_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    prompt_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("prompt_versions.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    campaign_candidate: Mapped["CampaignCandidate"] = relationship(back_populates="ai_evaluation")


class CandidateCompositeScoreHistory(Base):
    """
    M10-E01: one immutable row per composite_score calculation - an
    append-only audit trail distinct from campaign_candidates.composite_score
    itself (which only ever holds the latest value). Rows are never updated
    or deleted; every recalculation (AI evaluation completing, or a
    campaign weight change) inserts a new row. CompositeScoringService is
    the only writer of both this table and campaign_candidates.composite_score.
    """
    __tablename__ = "candidate_composite_score_history"
    __table_args__ = (
        Index("ix_composite_score_history_campaign_candidate_id", "campaign_candidate_id"),
        Index("ix_composite_score_history_calculated_at", "calculated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_candidate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("campaign_candidates.id"), nullable=False)
    deterministic_score: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True)
    # Raw 0-1 cosine similarity, exactly as stored on campaign_candidates.
    semantic_score: Mapped[Optional[float]] = mapped_column(Numeric(7, 6), nullable=True)
    # semantic_score normalized to the same 0-100 scale as
    # deterministic_score/effective_ai_score, for combination in the
    # formula - see CompositeScoringService.normalize_scores.
    normalized_semantic_score: Mapped[Optional[float]] = mapped_column(Numeric(7, 4), nullable=True)
    effective_ai_score: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True)
    # The campaign's configured weights at calculation time, used exactly
    # as-is - no redistribution. Missing score components are COALESCEd to
    # 0 (see CompositeScoringService.normalize_scores), never handled by
    # rescaling weights.
    weight_deterministic: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    weight_semantic: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    weight_ai: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    composite_score: Mapped[float] = mapped_column(Numeric(6, 3), nullable=False)
    formula_version: Mapped[str] = mapped_column(String(20), nullable=False)
    trigger_source: Mapped[CompositeScoreTriggerSource] = mapped_column(
        SAEnum(CompositeScoreTriggerSource, name="composite_score_trigger_source_enum"), nullable=False,
    )
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
