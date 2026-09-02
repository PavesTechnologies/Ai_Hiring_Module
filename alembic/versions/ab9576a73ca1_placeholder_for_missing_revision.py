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

2026-08-31 update: renamed from 7b3f6a92e1c4 to ab9576a73ca1. That id turned
out not to be missing at all - it collides with the real, already-committed
7b3f6a92e1c4_audit_log_immutability_trigger.py (down_revision c8e1a4f97d52),
which c1f4a7b93e20 and 43535e9e3cf7 both already chain onto by that id (see
docs/known_issues.md's 2026-08-13 "43535e9e3cf7" entry - both references were
verified there to mean the real migration, not this placeholder). Same
coincidental-collision shape as the 9a1c2f3e6b7d incident: `alembic heads`
started emitting "Revision 7b3f6a92e1c4 is present more than once" once both
files coexisted. Renamed this placeholder (a no-op, safe to rename) rather
than the real migration (which downstream files already depend on by id).
down_revision unchanged - still chains onto 9b2e4c7a1f38.

Revision ID: ab9576a73ca1
Revises: 9b2e4c7a1f38
Create Date: 2026-08-13
"""
from typing import Sequence, Union

revision: str = "ab9576a73ca1"
down_revision: Union[str, Sequence[str], None] = "9b2e4c7a1f38"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """No-op: this revision exists only to make the DB's stamp resolvable."""


def downgrade() -> None:
    """No-op: nothing was applied, so there is nothing to reverse."""
