"""campaign weight configuration history (M10-E02)

Creates campaign_weight_configuration_history, an append-only audit trail
of every Campaign Weight Configuration change - one row per actual weight
change (old_weight_*/new_weight_*, changed_by, changed_at, formula_version).
Distinct from hiring_campaigns.weight_* (which only ever holds the latest
values) and from candidate_composite_score_history (M10-E01, which tracks
per-candidate composite score calculations, not campaign-level weight
configuration changes).

Also adds the CAMPAIGN_WEIGHT_CONFIGURATION_CHANGED audit_action_type_enum
value CampaignService logs whenever a weight change actually happens,
mirroring COMPOSITE_SCORE_COMPUTED's existing role for M10-E01.

Revision ID: c2e6a1f8d4b7
Revises: b1f4c9a2e7d3
Create Date: 2026-08-03
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "c2e6a1f8d4b7"
down_revision = "b1f4c9a2e7d3"
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction block.
transactional_ddl = False


def upgrade() -> None:
    op.create_table(
        "campaign_weight_configuration_history",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("old_weight_deterministic", sa.Numeric(5, 2), nullable=False),
        sa.Column("old_weight_semantic", sa.Numeric(5, 2), nullable=False),
        sa.Column("old_weight_ai", sa.Numeric(5, 2), nullable=False),
        sa.Column("new_weight_deterministic", sa.Numeric(5, 2), nullable=False),
        sa.Column("new_weight_semantic", sa.Numeric(5, 2), nullable=False),
        sa.Column("new_weight_ai", sa.Numeric(5, 2), nullable=False),
        sa.Column("changed_by", sa.String(length=255), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("formula_version", sa.String(length=20), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["campaign_id"], ["hiring_campaigns.id"]),
        sa.ForeignKeyConstraint(["changed_by"], ["users.id"]),
    )
    op.create_index(
        "ix_campaign_weight_config_history_campaign_id",
        "campaign_weight_configuration_history",
        ["campaign_id"],
    )
    op.create_index(
        "ix_campaign_weight_config_history_changed_at",
        "campaign_weight_configuration_history",
        ["changed_at"],
    )

    op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'CAMPAIGN_WEIGHT_CONFIGURATION_CHANGED'")


def downgrade() -> None:
    op.drop_index("ix_campaign_weight_config_history_changed_at", table_name="campaign_weight_configuration_history")
    op.drop_index("ix_campaign_weight_config_history_campaign_id", table_name="campaign_weight_configuration_history")
    op.drop_table("campaign_weight_configuration_history")
    # PostgreSQL cannot drop a value from an enum type; leaving
    # CAMPAIGN_WEIGHT_CONFIGURATION_CHANGED in place is harmless.
