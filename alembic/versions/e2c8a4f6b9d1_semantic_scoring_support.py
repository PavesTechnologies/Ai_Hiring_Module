"""semantic scoring support (M08-E02)

Adds campaign_candidates.semantic_score_breakdown (JSONB) - the semantic-
layer analog of score_breakdown, storing overall_similarity/semantic_passed/
semantic_threshold/matching_skills/missing_skills/matched_keywords/
semantic_explanation exactly as score_breakdown already does for the
deterministic layer. campaign_candidates.semantic_score itself already
existed (Numeric(7,6)) and is reused as-is - only this one new column is
required, per the "no schema change unless absolutely necessary" scope.

Also adds the SEMANTIC_SCORE_COMPUTED audit_action_type_enum value the new
semantic scoring task logs on every run, mirroring
DETERMINISTIC_SCORE_COMPUTED's existing role for the deterministic layer.

Revision ID: e2c8a4f6b9d1
Revises: d3f7b9c2a1e5
Create Date: 2026-07-29
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "e2c8a4f6b9d1"
down_revision = "d3f7b9c2a1e5"
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction block.
transactional_ddl = False


def upgrade() -> None:
    op.add_column(
        "campaign_candidates",
        sa.Column("semantic_score_breakdown", JSONB, nullable=True),
    )
    op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'SEMANTIC_SCORE_COMPUTED'")


def downgrade() -> None:
    op.drop_column("campaign_candidates", "semantic_score_breakdown")
    # PostgreSQL cannot drop a value from an enum type; leaving it in place is harmless.
