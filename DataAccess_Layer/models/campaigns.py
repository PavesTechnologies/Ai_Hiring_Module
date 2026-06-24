import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, Enum as SAEnum, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from DataAccess_Layer.utils.db_connection import Base


class CampaignStatus(enum.Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    CLOSED = "CLOSED"


class HiringCampaign(Base):
    __tablename__ = "hiring_campaigns"
    __table_args__ = (
        CheckConstraint(
            "weight_deterministic + weight_semantic + weight_ai = 100",
            name="chk_weights_sum_100",
        ),
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
    max_candidates: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    deadline: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    hiring_manager_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
