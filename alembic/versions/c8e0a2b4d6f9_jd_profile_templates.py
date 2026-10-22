"""JD profile templates (Phase 3 Step 7)

- jd_profile_templates: FRESHER / EXPERIENCED / DOMAIN, each with defaults
  for internship credit, experience tolerance (NULL = system default) and
  education, and the resume sections that count as evidence. Seeded; the
  values are edited by HR_ADMIN (audited).
- job_descriptions.profile_template: chosen in JD calibration; NULL = none.
- audit: JD_PROFILE_TEMPLATE_UPDATED, entity JD_PROFILE_TEMPLATE.

Revision ID: c8e0a2b4d6f9
Revises: b7d9f1a3c5e7
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c8e0a2b4d6f9"
down_revision: Union[str, Sequence[str], None] = "b7d9f1a3c5e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STANDARD = ["SUMMARY", "EXPERIENCE", "SKILLS"]
_TEMPLATES = [
    {"key": "FRESHER", "label": "Fresher",
     "description": "Graduates and early-career roles: internships count in full, education is required, "
                    "and academic or personal projects count as evidence.",
     "internship_credit_pct": 100, "experience_tolerance_years": None, "education_requirement": "MANDATORY",
     "evidence_sections": _STANDARD + ["PROJECTS"]},
    {"key": "EXPERIENCED", "label": "Experienced",
     "description": "Roles that need professional experience: internships don't count and education is preferred.",
     "internship_credit_pct": 0, "experience_tolerance_years": None, "education_requirement": "PREFERRED",
     "evidence_sections": list(_STANDARD)},
    {"key": "DOMAIN", "label": "Domain",
     "description": "Platform or domain specialists (e.g. ServiceNow, ITSM): like Experienced; mapped "
                    "certifications still go through the vote.",
     "internship_credit_pct": 0, "experience_tolerance_years": None, "education_requirement": "PREFERRED",
     "evidence_sections": list(_STANDARD)},
]


def upgrade() -> None:
    templates = op.create_table(
        "jd_profile_templates",
        sa.Column("key", sa.String(20), primary_key=True),
        sa.Column("label", sa.String(60), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("internship_credit_pct", sa.SmallInteger(), nullable=False),
        sa.Column("experience_tolerance_years", sa.Numeric(4, 1), nullable=True),
        sa.Column("education_requirement", sa.String(20), nullable=False),
        sa.Column("evidence_sections", postgresql.JSONB(), nullable=False),
        sa.Column("updated_by", sa.String(255), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("key IN ('FRESHER', 'EXPERIENCED', 'DOMAIN')", name="ck_jd_profile_templates_key"),
        sa.CheckConstraint("internship_credit_pct IN (100, 50, 0)", name="ck_jd_profile_templates_internship"),
        sa.CheckConstraint("education_requirement IN ('MANDATORY', 'PREFERRED')",
                           name="ck_jd_profile_templates_education"),
        sa.CheckConstraint("experience_tolerance_years IS NULL OR "
                           "(experience_tolerance_years >= 0 AND experience_tolerance_years <= 20)",
                           name="ck_jd_profile_templates_tolerance"),
    )
    op.bulk_insert(templates, _TEMPLATES)

    op.add_column("job_descriptions", sa.Column(
        "profile_template", sa.String(20), sa.ForeignKey("jd_profile_templates.key",
                                                         name="fk_job_descriptions_profile_template"),
        nullable=True))

    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'JD_PROFILE_TEMPLATE_UPDATED'")
        op.execute("ALTER TYPE audit_entity_type_enum ADD VALUE IF NOT EXISTS 'JD_PROFILE_TEMPLATE'")


def downgrade() -> None:
    # Enum values stay (Postgres can't drop them; unused values are harmless).
    op.drop_constraint("fk_job_descriptions_profile_template", "job_descriptions", type_="foreignkey")
    op.drop_column("job_descriptions", "profile_template")
    op.drop_table("jd_profile_templates")
