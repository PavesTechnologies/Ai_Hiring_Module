"""JD calibration (Phase 2 Step 1)

- job_descriptions: calibration_status (NOT_CALIBRATED / CALIBRATED),
  calibrated_by / calibrated_at, calibration_version, the per-JD near-miss
  experience tolerance (NULL = default max(1 year, 10% of the minimum)) and
  education_requirement (MANDATORY / PREFERRED, default MANDATORY), and
  calibrated_min_experience_years (the extraction and min_experience_years
  are never overwritten; scoring reads the calibrated value first).
- jd_skills.sense_key: the HR-confirmed sense of an ambiguous skill.
- jd_skills verified by HR in calibration: verification status HR_VERIFIED
  plus verified_by / verified_at.
- jd_calibration_versions: one immutable snapshot per calibration save.
- hiring_campaigns.jd_calibration_exempt: campaigns that already exist are
  exempt from the "JD must be calibrated to activate" rule until their JD is
  calibrated; new campaigns default to not exempt.
- audit actions JD_CALIBRATED / JD_CALIBRATION_CHANGED.

Revision ID: c7e9a3f5b1d2
Revises: b5d7f9a1c3e6
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c7e9a3f5b1d2"
down_revision: Union[str, Sequence[str], None] = "b5d7f9a1c3e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ACTION_TYPES = ("JD_CALIBRATED", "JD_CALIBRATION_CHANGED")


def upgrade() -> None:
    calibration_status = postgresql.ENUM("NOT_CALIBRATED", "CALIBRATED", name="jd_calibration_status_enum")
    calibration_status.create(op.get_bind(), checkfirst=True)
    education_requirement = postgresql.ENUM("MANDATORY", "PREFERRED", name="jd_education_requirement_enum")
    education_requirement.create(op.get_bind(), checkfirst=True)

    op.add_column("job_descriptions", sa.Column(
        "calibration_status", postgresql.ENUM(name="jd_calibration_status_enum", create_type=False),
        nullable=False, server_default="NOT_CALIBRATED"))
    op.add_column("job_descriptions", sa.Column("calibrated_by", sa.String(255), sa.ForeignKey("users.id"), nullable=True))
    op.add_column("job_descriptions", sa.Column("calibrated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("job_descriptions", sa.Column("calibration_version", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("job_descriptions", sa.Column("experience_tolerance_years", sa.Numeric(4, 1), nullable=True))
    op.add_column("job_descriptions", sa.Column(
        "education_requirement", postgresql.ENUM(name="jd_education_requirement_enum", create_type=False),
        nullable=False, server_default="MANDATORY"))

    op.add_column("job_descriptions", sa.Column("calibrated_min_experience_years", sa.Numeric(4, 1), nullable=True))

    op.add_column("jd_skills", sa.Column("sense_key", sa.String(64), nullable=True))
    op.add_column("jd_skills", sa.Column("verified_by", sa.String(255), sa.ForeignKey("users.id"), nullable=True))
    op.add_column("jd_skills", sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "jd_calibration_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("jd_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("job_descriptions.id"), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("changes", postgresql.JSONB(), nullable=True),
        sa.Column("calibrated_by", sa.String(255), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("calibrated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("jd_id", "version_no", name="uq_jd_calibration_versions_jd_version"),
    )
    op.create_index("ix_jd_calibration_versions_jd_id", "jd_calibration_versions", ["jd_id"])

    # Existing campaigns are exempt (true); new ones are not (default false).
    op.add_column("hiring_campaigns", sa.Column(
        "jd_calibration_exempt", sa.Boolean(), nullable=False, server_default=sa.text("true")))
    op.alter_column("hiring_campaigns", "jd_calibration_exempt", server_default=sa.text("false"))

    with op.get_context().autocommit_block():
        for value in _ACTION_TYPES:
            op.execute(f"ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS '{value}'")
        op.execute("ALTER TYPE jd_skill_verification_status_enum ADD VALUE IF NOT EXISTS 'HR_VERIFIED'")


def downgrade() -> None:
    op.drop_column("hiring_campaigns", "jd_calibration_exempt")
    op.drop_index("ix_jd_calibration_versions_jd_id", table_name="jd_calibration_versions")
    op.drop_table("jd_calibration_versions")
    for column in ("verified_at", "verified_by", "sense_key"):
        op.drop_column("jd_skills", column)
    for column in ("calibrated_min_experience_years", "education_requirement", "experience_tolerance_years", "calibration_version",
                   "calibrated_at", "calibrated_by", "calibration_status"):
        op.drop_column("job_descriptions", column)
    postgresql.ENUM(name="jd_education_requirement_enum").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="jd_calibration_status_enum").drop(op.get_bind(), checkfirst=True)
    # Enum values (audit actions, HR_VERIFIED) can't be dropped in PostgreSQL; left in place.
