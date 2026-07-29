"""placeholder for missing revision

The database's alembic_version was found stamped at 'a1c6d9f3e7b2', a
revision that does not exist in any commit in this repo (checked main,
sathwik, and full history/reflog/stash/dangling-object search) — same class
of issue as 265912f5590a's "placeholder for missing revision" fix on
2026-07-15: someone applied a migration locally against the shared dev DB
without ever committing the migration file.

Chained after a4b7c1d9e2f5 (the add-prompt_templates-table migration) since
that is the last revision confirmed to match the live schema: the
prompt_templates table exists and its columns match the current model
exactly, and the resumes table also matches the current model (minus the
two columns 3ae81a38abdc adds on top of this). No unexplained columns were
found on either table, so this is treated as a no-op placeholder rather than
attempting to guess at a real schema change.

Revision ID: a1c6d9f3e7b2
Revises: a4b7c1d9e2f5
Create Date: 2026-07-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c6d9f3e7b2'
down_revision: Union[str, Sequence[str], None] = 'a4b7c1d9e2f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
