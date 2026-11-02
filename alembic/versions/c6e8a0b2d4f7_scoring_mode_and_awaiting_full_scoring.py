"""candidate_scoring_results.scoring_mode and awaiting_full_scoring

Revision ID: c6e8a0b2d4f7
Revises: a3c5e7f9b1d4
Create Date: 2026-11-02

Retire v1, phase B: the no-LLM fallback. When the daily LLM call cap or a provider failure stops
v2's core-skill votes, the candidate is scored with the scan, auto-confirm and rules only
(scoring_mode 'no_llm'). Such a result that held the candidate for review is marked
awaiting_full_scoring until the full (LLM) scoring has run when quota returns. Additive: two
NOT NULL columns with server defaults ('llm', false) and a partial index on the waiting rows.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c6e8a0b2d4f7"
down_revision: Union[str, None] = "a3c5e7f9b1d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("candidate_scoring_results",
                  sa.Column("scoring_mode", sa.String(20), nullable=False, server_default="llm"))
    op.add_column("candidate_scoring_results",
                  sa.Column("awaiting_full_scoring", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index("ix_candidate_scoring_results_awaiting_full", "candidate_scoring_results",
                    ["campaign_candidate_id"], postgresql_where=sa.text("awaiting_full_scoring"))


def downgrade() -> None:
    op.drop_index("ix_candidate_scoring_results_awaiting_full", table_name="candidate_scoring_results")
    op.drop_column("candidate_scoring_results", "awaiting_full_scoring")
    op.drop_column("candidate_scoring_results", "scoring_mode")
