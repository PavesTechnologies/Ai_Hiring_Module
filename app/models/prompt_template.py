import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class PromptTemplateStatus(enum.Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class PromptTemplate(Base):
    """
    One prompt template per task_type (AI_EVALUATE, JD_PARSE, RESUME_PARSE).
    content_hash/updated_at are maintained by PromptTemplateService, not by
    the model itself - see _compute_hash()/onupdate below.
    """

    __tablename__ = "prompt_templates"
    __table_args__ = (
        Index("idx_prompt_templates_status", "status"),
        Index("idx_prompt_templates_content_hash", "content_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_type: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    template_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[PromptTemplateStatus] = mapped_column(
        SAEnum(PromptTemplateStatus, name="prompt_template_status_enum"),
        nullable=False,
        default=PromptTemplateStatus.ACTIVE,
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_by: Mapped[Optional[str]] = mapped_column(String(255), ForeignKey("users.id"), nullable=True)
    # onupdate fires whenever this row is UPDATEd - the service only ever
    # flushes an UPDATE here when template_text/notes/status actually
    # changed, so this satisfies "auto-update updated_at on those changes"
    # without needing a DB trigger or event listener.
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), onupdate=func.now(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
