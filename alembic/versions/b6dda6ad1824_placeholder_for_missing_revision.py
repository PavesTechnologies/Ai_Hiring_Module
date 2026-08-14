"""placeholder for missing revision (merge)

RENAMED from 9a1c2f3e6b7d to b6dda6ad1824 (Epic 3 pre-work, 2026-08-11) -
the id this file originally claimed turned out not to be missing at all.
A teammate's real, already-merged migration
(9a1c2f3e6b7d_add_rejection_composite_trigger.py, down_revision
7043b9ed5abe) legitimately owns that id: it was genuinely applied via
`alembic upgrade` against the shared dev DB, which is exactly what
produced the "unknown revision" stamp this file was originally written to
paper over. This file's own investigation (below, otherwise unchanged)
happened before that migration's file existed in this checkout - by the
time it was pulled in, both files claimed the same id, which `alembic
heads` surfaced as "Revision 9a1c2f3e6b7d is present more than once." See
docs/known_issues.md's 2026-08-10 entry for the full writeup. The
teammate's file was NOT touched - only this one was renamed, and its
down_revision below now includes their revision as a third merge parent
instead of guessing at what the real change was.

alembic_version was found stamped at '9a1c2f3e6b7d' - a revision that does
not exist as a migration file anywhere in this repo (checked local history,
origin/main, and every fetched teammate branch) - same class of issue as
7043b9ed5abe's and a1c6d9f3e7b2's own "placeholder for missing revision"
fixes (see docs/known_issues.md): someone applied a schema change directly
against the shared dev DB and hand-stamped alembic_version without ever
committing the migration file.

Verified before authoring this: every schema effect from both of this
repo's current heads (09f831e39061: audit_entity_type_enum CANDIDATE/
PLATFORM_CONFIG; e686c750b7b4: email_trigger_event_enum M12 values) is
confirmed live on the target DB. campaign_candidate_stage_history itself
(the table E02's next migration touches) was checked directly and is
unaffected - whatever this stamp actually represents, it did not alter that
table. Authored as a merge of the 2 current heads into the missing revision
id the DB is already stamped with - a no-op (nothing here changes schema),
not a guess at what the real change was.

Re-verified at rename time: composite_score_trigger_source_enum's
'REJECTION' value (9a1c2f3e6b7d_add_rejection_composite_trigger.py's own
schema effect) is confirmed live too - consistent with that migration
having actually been applied for real, not just referenced. All 3 merge
parents' effects are independently confirmed live, same "verify every
branch tip before merging" methodology as every prior recurrence.

Revision ID: b6dda6ad1824
Revises: 09f831e39061, e686c750b7b4, 9a1c2f3e6b7d
Create Date: 2026-08-10
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b6dda6ad1824'
down_revision: Union[str, Sequence[str], None] = ('09f831e39061', 'e686c750b7b4', '9a1c2f3e6b7d')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
