"""Scoring v2 Step 3: campaign_candidates.score_out_of_date_at / _reason

Set when the candidate's resume was re-parsed from ANOTHER campaign: its
skills changed but this campaign's scores weren't recomputed. Cleared when
the candidate is next scored. Additive only.

Revision ID: e9a4c6b2d8f1
Revises: d7f1b3a9c5e2
Create Date: 2026-10-08
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e9a4c6b2d8f1"
down_revision: Union[str, Sequence[str], None] = "d7f1b3a9c5e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("campaign_candidates", sa.Column("score_out_of_date_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("campaign_candidates", sa.Column("score_out_of_date_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("campaign_candidates", "score_out_of_date_reason")
    op.drop_column("campaign_candidates", "score_out_of_date_at")
