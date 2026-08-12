import enum
import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, Enum as SAEnum, ForeignKey,
    Index, Integer, Numeric, String, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.database import Base
from app.models.jd.job_descriptions import JobDescription  # adjust import path to wherever JobDescription actually lives


class CampaignStatus(enum.Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    CLOSED = "CLOSED"


class HiringCampaign(Base):
    __tablename__ = "hiring_campaigns"
    __table_args__ = (
        CheckConstraint(
            "weight_deterministic + weight_semantic + weight_ai = 100.00",
            name="chk_weights_sum_100",
        ),
        UniqueConstraint("org_id", "name", name="uq_campaign_name_per_org"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=True)
    jd_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("job_descriptions.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[CampaignStatus] = mapped_column(SAEnum(CampaignStatus, name="campaign_status_enum"), nullable=False, default=CampaignStatus.ACTIVE)
    weight_deterministic: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, default=30.00)
    weight_semantic: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, default=40.00)
    weight_ai: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, default=30.00)
    semantic_threshold: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False, default=0.6500)
    ai_threshold: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, default=50.00)
    deterministic_threshold: Mapped[float] = mapped_column(
        Numeric(5, 2), nullable=False, default=70.00, server_default=text("70.00")
    )
    # Skill-stage qualification (core/supporting importance) - see
    # CandidateScoringService.evaluate_skill_qualification. 0.00 default
    # means "no coverage gate" (always satisfied) until a campaign
    # explicitly configures one - existing campaigns keep scoring exactly
    # as before this feature existed.
    required_skill_coverage_threshold: Mapped[float] = mapped_column(
        Numeric(5, 2), nullable=False, default=0.00, server_default=text("0.00")
    )
    # Fixed business limit (never proportional to required-skill count) -
    # see DEFAULT_MAX_MISSING_CORE_SKILLS.
    max_missing_core_skills: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default=text("3")
    )
    max_candidates: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    deadline: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    prompt_template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prompt_templates.id"), nullable=False, index=True
    )
    ai_evaluate_prompt_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prompt_templates.id"), nullable=True, index=True
    )
    hiring_manager_id: Mapped[str] = mapped_column(String(36), nullable=False)
    recruiter_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    report_scheduled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # 👇 New relationship — many-to-one, HiringCampaign.jd_id -> JobDescription.id
    job_description: Mapped["JobDescription"] = relationship(
        "JobDescription",
        foreign_keys=[jd_id],
        lazy="joined",   # sets the default; repository can still override per-query with joinedload/selectinload
    )


class CampaignWeightConfigurationHistory(Base):
    """
    M10-E02: one immutable row per Campaign Weight Configuration change -
    an append-only audit trail distinct from hiring_campaigns.weight_* itself
    (which only ever holds the latest values). Rows are never updated or
    deleted; every weight change (via update_scoring_configuration or
    update_campaign) inserts exactly one row capturing the before/after
    weights, who changed them, and the composite-score formula version in
    effect at the time. A no-op update (identical weights resubmitted)
    never reaches this table at all - CampaignService only calls its
    repository's create() when the weight fields actually changed.
    """
    __tablename__ = "campaign_weight_configuration_history"
    __table_args__ = (
        Index("ix_campaign_weight_config_history_campaign_id", "campaign_id"),
        Index("ix_campaign_weight_config_history_changed_at", "changed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hiring_campaigns.id"), nullable=False)
    old_weight_deterministic: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    old_weight_semantic: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    old_weight_ai: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    new_weight_deterministic: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    new_weight_semantic: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    new_weight_ai: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    changed_by: Mapped[Optional[str]] = mapped_column(String(255), ForeignKey("users.id"), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # Reuses app.enums.constants.COMPOSITE_SCORE_FORMULA_VERSION - never a
    # second/independent version constant (M10-E01 already established
    # this exact rule for candidate_composite_score_history).
    formula_version: Mapped[str] = mapped_column(String(20), nullable=False)