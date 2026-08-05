"""ranked candidate list index (M10-E03 Phase 1)

Adds a composite index on campaign_candidates(campaign_id, composite_score)
to support the ranked candidate list's default query shape -
`WHERE campaign_id = X ORDER BY composite_score DESC NULLS LAST` - without a
full scan of the campaign's rows followed by an in-memory sort. A plain
btree index is bidirectionally scannable, so this same index also serves
ASC ordering and composite_score_min/max range filters; no separate
DESC-specific index is needed.

No other schema change is required for Phase 1 - every other column this
epic reads (deterministic_score, semantic_score, effective_ai_score,
pipeline_stage, is_fraud_flagged, hr_override, ai_recommendation,
created_at, id) already exists on campaign_candidates.

Revision ID: f1a3c7e9b2d4
Revises: ee6515ea0cf6
Create Date: 2026-08-04
"""
from alembic import op

revision = "f1a3c7e9b2d4"
down_revision = "ee6515ea0cf6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_campaign_candidates_campaign_id_composite_score",
        "campaign_candidates",
        ["campaign_id", "composite_score"],
    )


def downgrade() -> None:
    op.drop_index("ix_campaign_candidates_campaign_id_composite_score", table_name="campaign_candidates")
