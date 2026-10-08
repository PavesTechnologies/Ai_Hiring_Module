"""Scoring v2 Step 3: per-campaign shadow switch, job-title pattern rules, re-parse audit actions

- hiring_campaigns.scoring_v2_shadow (default false): opt-in per campaign -
  v2 is computed and stored for comparison, never acted on.
- skill_pattern_rules.match_scope: LINE (resume text lines, default) or
  JOB_TITLE (parsed work-experience titles).
- audit_action_type_enum: RESUME_REPARSE_REQUESTED, RESUME_REPARSED.

Additive only.

Revision ID: d7f1b3a9c5e2
Revises: c4e8a2f6d9b1
Create Date: 2026-10-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d7f1b3a9c5e2"
down_revision: Union[str, Sequence[str], None] = "c4e8a2f6d9b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ACTION_TYPES = ("RESUME_REPARSE_REQUESTED", "RESUME_REPARSED")


def upgrade() -> None:
    op.add_column(
        "hiring_campaigns",
        sa.Column("scoring_v2_shadow", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "skill_pattern_rules",
        sa.Column("match_scope", sa.String(20), nullable=False, server_default=sa.text("'LINE'")),
    )
    with op.get_context().autocommit_block():
        for value in _ACTION_TYPES:
            op.execute(f"ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # Enum values can't be dropped in PostgreSQL; they are left in place.
    op.drop_column("skill_pattern_rules", "match_scope")
    op.drop_column("hiring_campaigns", "scoring_v2_shadow")
