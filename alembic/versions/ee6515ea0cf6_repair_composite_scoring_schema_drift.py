"""repair composite scoring schema drift

Guarded, idempotent repair for the M10-E01/M10-E02 objects that
b1f4c9a2e7d3 (composite scoring support) and c2e6a1f8d4b7 (campaign weight
configuration history) introduce in source control. Because those two
revisions were an unmerged Alembic head (see a558bcbcdb92), they were never
applied via `alembic upgrade head` and, per team practice documented in
docs/resume_intake_implementation_log.md, migrations on this multi-headed
chain are applied "by hand" instead - which is exactly how
`campaign_candidates.composite_score_computed_at does not exist` happened:
the column was added to the ORM model and to a committed migration file,
but that migration was never actually run against this database.

Every change below is guarded, following the exact pattern d88f9123b149
established for the same class of problem:

- On a database that already has these objects (because b1f4c9a2e7d3/
  c2e6a1f8d4b7 *were* hand-applied there, or because it was built fresh by
  replaying the full merged history from scratch), every guard is false and
  this revision is a no-op.
- On a database where they are missing (the reported failure), each object
  is created to exactly match the ORM models
  (app/models/pipeline.py:CampaignCandidate.composite_score_computed_at,
  app/models/campaigns.py:CampaignWeightConfigurationHistory) and the
  original migration DDL (b1f4c9a2e7d3, c2e6a1f8d4b7).

No existing data is touched or dropped.

Revision ID: ee6515ea0cf6
Revises: a558bcbcdb92
Create Date: 2026-08-04 00:00:01.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

# revision identifiers, used by Alembic.
revision: str = 'ee6515ea0cf6'
down_revision: Union[str, Sequence[str], None] = 'a558bcbcdb92'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction block.
transactional_ddl = False


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    existing_enums = {
        row[0]
        for row in bind.execute(
            sa.text("SELECT typname FROM pg_type WHERE typtype = 'e'")
        )
    }

    # 1. campaign_candidates.composite_score_computed_at (b1f4c9a2e7d3)
    campaign_candidates_cols = {c["name"] for c in inspector.get_columns("campaign_candidates")}
    if "composite_score_computed_at" not in campaign_candidates_cols:
        op.add_column(
            "campaign_candidates",
            sa.Column("composite_score_computed_at", sa.DateTime(timezone=True), nullable=True),
        )

    # 2. candidate_composite_score_history table + enum + indexes (b1f4c9a2e7d3)
    if "composite_score_trigger_source_enum" not in existing_enums:
        sa.Enum(
            "AI_EVALUATION", "CAMPAIGN_WEIGHT_CHANGE",
            name="composite_score_trigger_source_enum",
        ).create(bind, checkfirst=True)

    if "candidate_composite_score_history" not in existing_tables:
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

    # 3. audit_action_type_enum new value (b1f4c9a2e7d3) - IF NOT EXISTS is
    # already idempotent, no guard needed.
    op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'COMPOSITE_SCORE_COMPUTED'")

    # 4. campaign_weight_configuration_history table + indexes (c2e6a1f8d4b7)
    existing_tables = set(inspector.get_table_names())  # refresh after step 2
    if "campaign_weight_configuration_history" not in existing_tables:
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

    # 5. audit_action_type_enum new value (c2e6a1f8d4b7) - IF NOT EXISTS is
    # already idempotent, no guard needed.
    op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'CAMPAIGN_WEIGHT_CONFIGURATION_CHANGED'")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "campaign_weight_configuration_history" in set(inspector.get_table_names()):
        op.drop_index("ix_campaign_weight_config_history_changed_at", table_name="campaign_weight_configuration_history")
        op.drop_index("ix_campaign_weight_config_history_campaign_id", table_name="campaign_weight_configuration_history")
        op.drop_table("campaign_weight_configuration_history")

    if "candidate_composite_score_history" in set(inspector.get_table_names()):
        op.drop_index("ix_composite_score_history_calculated_at", table_name="candidate_composite_score_history")
        op.drop_index("ix_composite_score_history_campaign_candidate_id", table_name="candidate_composite_score_history")
        op.drop_table("candidate_composite_score_history")

    sa.Enum(name="composite_score_trigger_source_enum").drop(bind, checkfirst=True)

    campaign_candidates_cols = {c["name"] for c in inspector.get_columns("campaign_candidates")}
    if "composite_score_computed_at" in campaign_candidates_cols:
        op.drop_column("campaign_candidates", "composite_score_computed_at")
    # PostgreSQL cannot drop a value from an enum type; leaving
    # COMPOSITE_SCORE_COMPUTED/CAMPAIGN_WEIGHT_CONFIGURATION_CHANGED in place
    # is harmless, exactly as the original migrations noted.
