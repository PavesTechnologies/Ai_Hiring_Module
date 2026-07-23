"""merge resume/skill-ontology/bulk-upload branches

Pure bookkeeping merge, no DDL: three parallel branches (skill-ontology
audit actions, JD original_filename, bulk-ZIP-upload schema) were built
against b6e2d9a41c3f/d5c1a0b2e3f4 in parallel with a7c4e9f1d2b8 (resume
pipeline audit enums), all four became heads once merged into one branch,
and every statement in all three non-a7c4e9f1d2b8 branches was independently
verified already applied to the live DB (same undocumented-direct-DDL
pattern d88f9123b149/f3a6c9d1b7e2 describe) before this revision was
stamped rather than upgraded - see the DDL-presence verification in this
session's history. upgrade()/downgrade() are intentionally empty.

Revision ID: 3e7800c51995
Revises: a7c4e9f1d2b8, 4fcdbc589048, e7a4f2c9d8b1, f2c9b8e4a1d3
Create Date: 2026-07-17 16:54:24.336511

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3e7800c51995'
down_revision: Union[str, Sequence[str], None] = ('a7c4e9f1d2b8', '4fcdbc589048', 'e7a4f2c9d8b1', 'f2c9b8e4a1d3')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
