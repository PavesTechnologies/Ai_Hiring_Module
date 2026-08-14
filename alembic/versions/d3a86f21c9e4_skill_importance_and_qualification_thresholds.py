"""skill importance and qualification thresholds

Skill Qualification Enhancement (core/supporting importance):

- jd_skills.importance (nullable jd_skill_importance_enum: CORE/SUPPORTING).
  AI-classified once, at JD extraction time, for required (mandatory=True)
  skills only - always NULL for preferred skills. NULL also covers every
  pre-existing jd_skills row (legacy JDs processed before this feature
  existed) - CandidateScoringService treats a NULL importance as a neutral
  1.0 multiplier, never as an implicit "supporting", so no backfill/
  reprocessing of historical JDs is required for this migration to be safe.

- hiring_campaigns.required_skill_coverage_threshold (Numeric(5,2), default
  0.00 - "no coverage gate" until a campaign explicitly configures one) and
  hiring_campaigns.max_missing_core_skills (Integer, default 3 - the fixed,
  non-proportional business limit) - the two new per-campaign qualification
  thresholds, added following the exact same per-campaign-column pattern
  already used by deterministic_threshold/semantic_threshold on this table.

Purely additive: no existing column altered or dropped, no existing row's
data changed (every existing hiring_campaigns row picks up the new columns'
defaults; every existing jd_skills row gets importance = NULL).

jd_skills.weight is deliberately NOT touched by this migration - it keeps
meaning exactly what it already meant.

Revision ID: d3a86f21c9e4
Revises: 08655d0b0117
Create Date: 2026-08-11
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "d3a86f21c9e4"
down_revision = "08655d0b0117"
branch_labels = None
depends_on = None


jd_skill_importance_enum = postgresql.ENUM("CORE", "SUPPORTING", name="jd_skill_importance_enum")


def upgrade() -> None:
    jd_skill_importance_enum.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "jd_skills",
        sa.Column("importance", jd_skill_importance_enum, nullable=True),
    )

    op.add_column(
        "hiring_campaigns",
        sa.Column(
            "required_skill_coverage_threshold",
            sa.Numeric(precision=5, scale=2),
            nullable=False,
            server_default="0.00",
        ),
    )
    op.add_column(
        "hiring_campaigns",
        sa.Column(
            "max_missing_core_skills",
            sa.Integer(),
            nullable=False,
            server_default="3",
        ),
    )


def downgrade() -> None:
    op.drop_column("hiring_campaigns", "max_missing_core_skills")
    op.drop_column("hiring_campaigns", "required_skill_coverage_threshold")
    op.drop_column("jd_skills", "importance")
    jd_skill_importance_enum.drop(op.get_bind(), checkfirst=True)
