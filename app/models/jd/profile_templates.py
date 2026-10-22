"""
JD profile templates (Phase 3 Step 7): Fresher / Experienced / Domain.

A template bundles calibration defaults - internship credit, experience
tolerance, education mandatory or preferred - and the resume sections that
count as evidence (auto-confirm and paraphrase). HR picks one in JD
calibration (suggested from the JD); a JD's own settings always win. The
three rows are fixed (seeded); HR_ADMIN edits their values (audited).
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Numeric, SmallInteger, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class JDProfileTemplate(Base):
    __tablename__ = "jd_profile_templates"

    key: Mapped[str] = mapped_column(String(20), primary_key=True)          # FRESHER / EXPERIENCED / DOMAIN
    label: Mapped[str] = mapped_column(String(60), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    internship_credit_pct: Mapped[int] = mapped_column(SmallInteger, nullable=False)        # 100 / 50 / 0
    # NULL = the system default, max(1 year, 10% of the minimum).
    experience_tolerance_years: Mapped[Optional[float]] = mapped_column(Numeric(4, 1), nullable=True)
    education_requirement: Mapped[str] = mapped_column(String(20), nullable=False)        # MANDATORY / PREFERRED
    # Resume sections that count as evidence, e.g. ["SUMMARY", "EXPERIENCE", "SKILLS", "PROJECTS"].
    evidence_sections: Mapped[list] = mapped_column(JSONB, nullable=False)
    updated_by: Mapped[Optional[str]] = mapped_column(String(255), ForeignKey("users.id"), nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True,
                                                           onupdate=func.now())
