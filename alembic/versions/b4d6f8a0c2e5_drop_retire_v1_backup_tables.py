"""drop the retire-v1 backup tables (end of retire v1, phase E)

Revision ID: b4d6f8a0c2e5
Revises: a3c5e7f9b2d4
Create Date: 2026-11-03

Approved 2026-11-03. Nothing reads either table and no foreign key points at them:
- campaign_candidate_breakdown_backup (d8f0a2c4e6b9): the v1-shaped breakdowns replaced in phase C.
  Dev: 34 rows, all 34 candidates still exist, each backup's nested v2 equals the current breakdown.
- platform_config_backup (a3c5e7f9b2d4): the 6 v1-only settings deleted in phase E (their dev values are
  in a3c5e7f9b2d4's docstring).

d8f0a2c4e6b9 and a3c5e7f9b2d4 become irreversible: their downgrades stop with "irreversible: backup table
dropped". This migration's downgrade re-creates the two tables empty (their old schema), so a downgrade
past it reaches those clear stops instead of failing on a missing table.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b4d6f8a0c2e5"
down_revision: Union[str, None] = "a3c5e7f9b2d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("platform_config_backup")
    op.drop_table("campaign_candidate_breakdown_backup")


def downgrade() -> None:
    op.create_table(
        "campaign_candidate_breakdown_backup",
        sa.Column("campaign_candidate_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("deterministic_breakdown", postgresql.JSONB(), nullable=False),
        sa.Column("reason", sa.String(60), nullable=False),
        sa.Column("saved_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_table(
        "platform_config_backup",
        sa.Column("key", sa.String(255), primary_key=True),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_by", sa.String(255), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("saved_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
