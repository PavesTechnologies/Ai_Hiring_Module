"""scoring_version_golden_runs: golden-set runs for a scoring version, started from the Settings page

Revision ID: c9e1f3a5b7d2
Revises: a7c9e1b3d5f0
Create Date: 2026-11-12

Settings phase 3 (approved 2026-11-12). Additive: a new table. Promotion needs a PASSED run whose
config_hash is the version's exact configuration and model. Downgrade drops it.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c9e1f3a5b7d2"
down_revision: Union[str, None] = "a7c9e1b3d5f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scoring_version_golden_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("scoring_version_id", sa.UUID(), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("purpose", sa.String(length=20), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=12), server_default=sa.text("'QUEUED'"), nullable=False),
        sa.Column("expected_llm_calls", sa.Integer(), nullable=True),
        sa.Column("llm_calls", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("stop_reason", sa.String(length=40), nullable=True),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("requested_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["scoring_version_id"], ["scoring_versions.id"]),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_golden_runs_version_created", "scoring_version_golden_runs",
                    ["scoring_version_id", "created_at"])


def downgrade() -> None:
    op.drop_index("idx_golden_runs_version_created", table_name="scoring_version_golden_runs")
    op.drop_table("scoring_version_golden_runs")
