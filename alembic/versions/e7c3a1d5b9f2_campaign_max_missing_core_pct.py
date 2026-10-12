"""hiring_campaigns.max_missing_core_pct: proportional missing-core-skill limit

A candidate may now miss floor(core skills x max_missing_core_pct / 100)
core skills, capped at max_missing_core_skills - instead of a fixed count
that was almost no gate for a JD with 4 core skills. Existing campaigns get
the 25% default.

Revision ID: e7c3a1d5b9f2
Revises: d4b7e2a9c1f6
Create Date: 2026-09-30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e7c3a1d5b9f2"
down_revision: Union[str, Sequence[str], None] = "d4b7e2a9c1f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "hiring_campaigns",
        sa.Column("max_missing_core_pct", sa.Numeric(5, 2), nullable=False, server_default=sa.text("25.00")),
    )


def downgrade() -> None:
    op.drop_column("hiring_campaigns", "max_missing_core_pct")
