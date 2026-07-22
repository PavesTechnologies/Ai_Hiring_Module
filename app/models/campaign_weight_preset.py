from datetime import datetime
from decimal import Decimal
from uuid import uuid4
 
from sqlalchemy import Column, DateTime, ForeignKey, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
 
from app.db.database import Base
 
 
class CampaignWeightPreset(Base):
    __tablename__ = "campaign_weight_presets"
 
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "name",
            name="uq_campaign_weight_presets_org_name",
        ),
    )
 
    id: Mapped[UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
 
    org_id: Mapped[UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
 
    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
 
    description: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
 
    weight_deterministic: Mapped[Decimal] = mapped_column(
        Numeric(5, 2),
        nullable=False,
    )
 
    weight_semantic: Mapped[Decimal] = mapped_column(
        Numeric(5, 2),
        nullable=False,
    )
 
    weight_ai: Mapped[Decimal] = mapped_column(
        Numeric(5, 2),
        nullable=False,
    )

    deterministic_threshold: Mapped[float] = mapped_column(
        Numeric(5, 2),
        nullable=False,
        default=70.00,
    )
 
    semantic_threshold: Mapped[Decimal] = mapped_column(
        Numeric(5, 2),
        nullable=False,
    )
 
    ai_threshold: Mapped[Decimal] = mapped_column(
        Numeric(5, 2),
        nullable=False,
    )
 
    created_by: Mapped[str] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
    )
 
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
 