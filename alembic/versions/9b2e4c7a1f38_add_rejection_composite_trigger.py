"""Add REJECTION value to composite_score_trigger_source_enum

Revision ID: 9b2e4c7a1f38
Revises: d3a86f21c9e4
Create Date: 2026-08-10 00:00:00.000000

Renumbered from 9a1c2f3e6b7d, which a merge left claiming the same revision id
as 9a1c2f3e6b7d_placeholder_for_missing_revision.py — alembic refuses to
resolve a graph with a duplicate id. Re-parented onto d3a86f21c9e4 (the tip of
that branch) rather than 7043b9ed5abe so it does not open a new head, and the
ADD VALUE below is now IF NOT EXISTS: the REJECTION label was verified already
present on the target DB, so this must be safe to re-run. See
docs/known_issues.md, "Alembic: multiple unmerged migration heads".
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '9b2e4c7a1f38'
down_revision: Union[str, Sequence[str], None] = 'd3a86f21c9e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Postgres 12+ allows ALTER TYPE ... ADD VALUE inside a transaction as
    # long as the new value isn't used in that same transaction - fine here,
    # since nothing in this migration inserts a row using it.
    op.execute(
        "ALTER TYPE composite_score_trigger_source_enum "
        "ADD VALUE IF NOT EXISTS 'REJECTION'"
    )


def downgrade() -> None:
    # Postgres has no ALTER TYPE ... DROP VALUE - removing an enum value
    # requires rebuilding the type (rename old -> create new -> cast every
    # dependent column -> drop old), which is only worth doing if some row
    # actually used 'REJECTION'. Left as a manual/no-op downgrade since this
    # revision is additive-only and never removes data on its own.
    pass
