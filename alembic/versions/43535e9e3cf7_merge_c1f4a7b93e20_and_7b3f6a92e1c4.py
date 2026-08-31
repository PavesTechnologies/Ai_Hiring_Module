"""merge heads: c1f4a7b93e20 (M11 saved views/skill search) + 7b3f6a92e1c4 (Epic 3 Fix 4)

Genuine parallel-branch merge, not a phantom/collision fix - see
docs/known_issues.md's "c1f4a7b93e20 is a second unmerged head" entry for
the full writeup. Two people validly working on two independent branches
of the same migration chain at the same time on a shared dev instance:
this branch's chain (08655d0b0117 -> d3a86f21c9e4 [skill importance,
niharika's real migration, see known_issues.md] -> c8e1a4f97d52 [Epic 3
Fix 3] -> 7b3f6a92e1c4 [Epic 3 Fix 4]) and c1f4a7b93e20 (M11-E03 saved
views/skill search + M11-E04 recruiter notes, also niharika's, PR #94)
both branch off the same root (7043b9ed5abe) and neither depends on the
other's schema.

Verified live before writing this (2026-08-13, full recursive model-vs-DB
drift audit - tables/columns/enums, not just the specific items named
below): both branches' complete schema effects are already live -
user_saved_views, candidate_notes, search_queries.canonical_skill_ids,
the 3 CANDIDATE_NOTE_* audit_action_type_enum values (c1f4a7b93e20), and
every Epic 3 Fix 1-5 effect (audit_log indexes/triggers, the
idempotency_key column, etc. - 7b3f6a92e1c4's own chain). This is a
pure bookkeeping merge - stamped, not upgraded, since nothing needs to
run. The audit found one further, unrelated drift (hiring_campaigns.
scheduled_export_config + 8 new audit_action_type_enum *_EXPORTED/DSAR_*
values) with no file anywhere in any fetched branch - tracked separately,
not folded into this merge (see the dedicated known_issues.md entry and
placeholder migration chained on top of this one).

Revision ID: 43535e9e3cf7
Revises: 7b3f6a92e1c4, c1f4a7b93e20
Create Date: 2026-08-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '43535e9e3cf7'
down_revision: Union[str, Sequence[str], None] = ('7b3f6a92e1c4', 'c1f4a7b93e20')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
