"""
Related education fields per role domain (Phase 3 Step 2).

Each row says two fields of study are one family for JDs of a role domain:
e.g. COMPUTER_SCIENCE <-> ELECTRONICS_ENGINEERING on a SOFTWARE JD. The pair
is unordered - stored with field_a < field_b so it can't be entered twice.
Used by the education gate when the JD's education is mandatory. Edited by
HR_ADMIN (audited).
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class EducationFieldRelation(Base):
    __tablename__ = "education_field_relations"
    __table_args__ = (
        UniqueConstraint("field_a", "field_b", "role_domain", name="uq_education_field_relations_pair_domain"),
        CheckConstraint("field_a < field_b", name="ck_education_field_relations_ordered"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    field_a: Mapped[str] = mapped_column(String(40), nullable=False)
    field_b: Mapped[str] = mapped_column(String(40), nullable=False)
    role_domain: Mapped[str] = mapped_column(String(20), nullable=False)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(255), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
