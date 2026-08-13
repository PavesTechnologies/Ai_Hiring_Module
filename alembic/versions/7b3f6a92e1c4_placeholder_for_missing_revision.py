"""placeholder for missing revision

alembic_version on the RDS dev DB is stamped at '7b3f6a92e1c4', a revision that
exists as no migration file in this repo. Same recurring pattern documented in
docs/known_issues.md ("Alembic: multiple unmerged migration heads") and already
papered over by 7043b9ed5abe's and 9a1c2f3e6b7d's own placeholder migrations:
someone applied a schema change straight against the shared dev DB and
hand-stamped alembic_version without committing the migration.

Without a node for that id, `alembic current` fails outright with "Can't locate
revision identified by '7b3f6a92e1c4'", which blocks every upgrade — including
ones that have nothing to do with whatever the stamp represents.

Verified before authoring: this changes no schema. It only gives the graph a
node for the id the database already claims to be at, chained onto
9b2e4c7a1f38 (the tip of the only remaining branch) so the history collapses to
a single head instead of adding another.

Revision ID: 7b3f6a92e1c4
Revises: 9b2e4c7a1f38
Create Date: 2026-08-13
"""
from typing import Sequence, Union

revision: str = "7b3f6a92e1c4"
down_revision: Union[str, Sequence[str], None] = "9b2e4c7a1f38"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """No-op: this revision exists only to make the DB's stamp resolvable."""


def downgrade() -> None:
    """No-op: nothing was applied, so there is nothing to reverse."""
