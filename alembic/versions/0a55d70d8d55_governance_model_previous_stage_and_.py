"""governance model: previous_stage + stage_transition_log

2026-08-31: context-aware transitions - AllowedTransition rows can now be
scoped to a specific previous_stage (e.g. HM_REVIEW ownership flipping to
HIRING_MANAGER regardless of current_stage), not just a flat
(from_stage, to_stage) edge. NULL previous_stage means "applies regardless
of previous_stage" (a wildcard) - see AllowedTransition's model docstring.

Applied directly against a freshly-rebuilt, empty dev DB (no data to
migrate) - see docs/known_issues.md-style history in the two placeholder
migrations immediately below this one in the graph for why the dev DB was
rebuilt from scratch rather than migrated in place.

Revision ID: 0a55d70d8d55
Revises: 57bdf7a71651
Create Date: 2026-08-31
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0a55d70d8d55'
down_revision: Union[str, Sequence[str], None] = '57bdf7a71651'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PIPELINE_STAGE_ENUM = postgresql.ENUM(name="pipeline_stage_enum", create_type=False)


def upgrade() -> None:
    op.add_column(
        "campaign_candidates",
        sa.Column("previous_stage", PIPELINE_STAGE_ENUM, nullable=True),
    )

    op.add_column(
        "allowed_transitions",
        sa.Column("previous_stage", PIPELINE_STAGE_ENUM, nullable=True),
    )
    op.drop_constraint("allowed_transitions_from_stage_to_stage_key", "allowed_transitions", type_="unique")
    op.create_unique_constraint(
        "uq_allowed_transitions_previous_from_to",
        "allowed_transitions",
        ["previous_stage", "from_stage", "to_stage"],
    )
    op.create_index(
        "uq_allowed_transitions_from_to_no_previous",
        "allowed_transitions",
        ["from_stage", "to_stage"],
        unique=True,
        postgresql_where=sa.text("previous_stage IS NULL"),
    )

    op.create_table(
        "stage_transition_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("campaign_candidate_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("campaign_candidates.id"), nullable=False),
        sa.Column("previous_stage", PIPELINE_STAGE_ENUM, nullable=True),
        sa.Column("from_stage", PIPELINE_STAGE_ENUM, nullable=False),
        sa.Column("to_stage", PIPELINE_STAGE_ENUM, nullable=False),
        sa.Column("role_used", sa.String(length=50), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("performed_by", sa.String(length=255), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_stage_transition_log_campaign_candidate_id",
        "stage_transition_log",
        ["campaign_candidate_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_stage_transition_log_campaign_candidate_id", table_name="stage_transition_log")
    op.drop_table("stage_transition_log")

    op.drop_index("uq_allowed_transitions_from_to_no_previous", table_name="allowed_transitions")
    op.drop_constraint("uq_allowed_transitions_previous_from_to", "allowed_transitions", type_="unique")
    op.create_unique_constraint(
        "allowed_transitions_from_stage_to_stage_key",
        "allowed_transitions",
        ["from_stage", "to_stage"],
    )
    op.drop_column("allowed_transitions", "previous_stage")

    op.drop_column("campaign_candidates", "previous_stage")
