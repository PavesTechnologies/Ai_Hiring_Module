"""merge heads: 02383ea4b4fd + ab9576a73ca1 + b4e7c2a91f38

2026-08-31: `alembic heads` resolved to 3 separate heads once the two
structural graph bugs above were fixed (dangling 9a1c2f3e6b7d reference in
b6dda6ad1824, duplicate 7b3f6a92e1c4 id renamed to ab9576a73ca1) - see those
two migrations' docstrings for the full history. This merge collapses them
back to a single head so `alembic upgrade head` is unambiguous again, ahead
of a full dev-DB drop-and-rebuild:

- 02383ea4b4fd: the main chain, and the revision the live dev DB was
  actually stamped at before the rebuild (interview_schedule_timezone).
- ab9576a73ca1: the renamed placeholder's own branch (9b2e4c7a1f38's tip -
  skill_importance/rejection-composite-trigger chain), never merged back in.
- b4e7c2a91f38: skill_ontology_parent_skill_id_index, a sibling branch off
  a1b2c3d4e5f6 that was likewise never merged back into the main chain, and
  was not part of the live dev DB's stamped history.

Not a bookkeeping/stamp-only merge like the prior recurrences in
docs/known_issues.md - the target DB is being dropped and rebuilt from
scratch via `alembic upgrade head`, so every migration under all 3 branches
actually runs for real here rather than being verified-live and stamped.
No-op merge point itself; the real schema changes live in the migrations
being merged, not in this file.

Revision ID: 57bdf7a71651
Revises: 02383ea4b4fd, ab9576a73ca1, b4e7c2a91f38
Create Date: 2026-08-31
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '57bdf7a71651'
down_revision: Union[str, Sequence[str], None] = ('02383ea4b4fd', 'ab9576a73ca1', 'b4e7c2a91f38')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
