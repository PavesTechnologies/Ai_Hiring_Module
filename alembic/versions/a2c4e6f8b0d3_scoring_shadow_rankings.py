"""Scoring v2 Step 4: scoring_shadow_rankings (a recruiter's own ranking, entered to compare with v1/v2)

Additive only.

Revision ID: a2c4e6f8b0d3
Revises: f3b5d7a9c1e4
Create Date: 2026-10-08
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a2c4e6f8b0d3"
down_revision: Union[str, Sequence[str], None] = "f3b5d7a9c1e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scoring_shadow_rankings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("hiring_campaigns.id"), nullable=False),
        sa.Column("campaign_candidate_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("campaign_candidates.id"),
                  nullable=False, unique=True),
        sa.Column("recruiter_rank", sa.Integer(), nullable=True),
        sa.Column("ranked_by_name", sa.String(150), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("entered_by", sa.String(255), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_scoring_shadow_rankings_campaign_id", "scoring_shadow_rankings", ["campaign_id"])


def downgrade() -> None:
    op.drop_index("ix_scoring_shadow_rankings_campaign_id", table_name="scoring_shadow_rankings")
    op.drop_table("scoring_shadow_rankings")
