"""composite scoring support (M10-E01)

Adds campaign_candidates.composite_score_computed_at (timestamp of the
latest composite_score calculation - composite_score itself already existed
and is reused as-is). Creates candidate_composite_score_history, an
append-only audit trail of every composite_score calculation (one row per
calculation, never updated), backing CompositeScoringService.

Composite Score has exactly two valid triggers - AI Evaluation completing
and a campaign's scoring weights changing (never an HR override, which only
restarts the remaining scoring pipeline) - hence trigger_source only has two
values. Weights are stored exactly as configured on the campaign; there is
no redistribution, so no "normalized weight" columns exist here - only
normalized_semantic_score (the 0-1 raw semantic_score rescaled to 0-100 for
combination with the other two layers).

Also adds the COMPOSITE_SCORE_COMPUTED audit_action_type_enum value the new
composite scoring task logs on every run, mirroring
SEMANTIC_SCORE_COMPUTED/DETERMINISTIC_SCORE_COMPUTED's existing role for the
other two scoring layers.

NOTE: this repo currently has multiple concurrent Alembic heads (verified via
`alembic heads`). This migration branches off e2c8a4f6b9d1 (the most recent
scoring-related head) rather than attempting to merge every unrelated head -
that merge is a separate, pre-existing concern outside this epic's scope.

Revision ID: b1f4c9a2e7d3
Revises: e2c8a4f6b9d1
Create Date: 2026-08-03
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "b1f4c9a2e7d3"
down_revision = "e2c8a4f6b9d1"
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction block.
transactional_ddl = False


def upgrade() -> None:
    op.add_column(
        "campaign_candidates",
        sa.Column("composite_score_computed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "candidate_composite_score_history",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_candidate_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("deterministic_score", sa.Numeric(5, 2), nullable=True),
        sa.Column("semantic_score", sa.Numeric(7, 6), nullable=True),
        sa.Column("normalized_semantic_score", sa.Numeric(7, 4), nullable=True),
        sa.Column("effective_ai_score", sa.Numeric(5, 2), nullable=True),
        sa.Column("weight_deterministic", sa.Numeric(5, 2), nullable=False),
        sa.Column("weight_semantic", sa.Numeric(5, 2), nullable=False),
        sa.Column("weight_ai", sa.Numeric(5, 2), nullable=False),
        sa.Column("composite_score", sa.Numeric(6, 3), nullable=False),
        sa.Column("formula_version", sa.String(length=20), nullable=False),
        sa.Column(
            "trigger_source",
            sa.Enum(
                "AI_EVALUATION", "CAMPAIGN_WEIGHT_CHANGE",
                name="composite_score_trigger_source_enum",
            ),
            nullable=False,
        ),
        sa.Column("calculated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["campaign_candidate_id"], ["campaign_candidates.id"]),
    )
    op.create_index(
        "ix_composite_score_history_campaign_candidate_id",
        "candidate_composite_score_history",
        ["campaign_candidate_id"],
    )
    op.create_index(
        "ix_composite_score_history_calculated_at",
        "candidate_composite_score_history",
        ["calculated_at"],
    )

    op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'COMPOSITE_SCORE_COMPUTED'")


def downgrade() -> None:
    op.drop_index("ix_composite_score_history_calculated_at", table_name="candidate_composite_score_history")
    op.drop_index("ix_composite_score_history_campaign_candidate_id", table_name="candidate_composite_score_history")
    op.drop_table("candidate_composite_score_history")
    sa.Enum(name="composite_score_trigger_source_enum").drop(op.get_bind(), checkfirst=True)
    op.drop_column("campaign_candidates", "composite_score_computed_at")
    # PostgreSQL cannot drop a value from an enum type; leaving
    # COMPOSITE_SCORE_COMPUTED in place is harmless.
