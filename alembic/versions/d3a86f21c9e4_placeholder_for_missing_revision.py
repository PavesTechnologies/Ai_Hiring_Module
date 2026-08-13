"""placeholder for missing revision (schema drift)

alembic_version was found stamped at 'd3a86f21c9e4' - a revision that does
not exist as a migration file anywhere in this repo. Checked exhaustively
(Epic 3 pre-work, 2026-08-11), same standard as every prior recurrence but
wider this time since there was no candidate teammate branch to check
against: `git fetch --all` (pulled fresh commits on main/RMe), then
searched every local and remote branch tip (authentication, epic1, main,
swarnaraj, origin/RMe, origin/loki, origin/main, origin/niharika,
origin/sathwik, origin/swarnaraj) both by filename and by content, and the
full commit history (`git log --all --diff-filter=A`) for this id. Zero
hits anywhere. Unlike the 9a1c2f3e6b7d incident (docs/known_issues.md),
this is not a collision with a real migration that simply hadn't been
pulled yet - no file with this id exists anywhere to pull.

Verified via a full drift audit instead of guessing: recursively imported
every SQLAlchemy model module and compared all 43 live tables and all 34
live enum types against the ORM. Tables and enums matched perfectly
(zero drift) except for exactly 3 items, all confirmed live, all
confirmed referenced by zero code anywhere in app/:

  - hiring_campaigns.max_missing_core_skills   (integer, NOT NULL, default 3)
  - hiring_campaigns.required_skill_coverage_threshold
                                                (numeric, NOT NULL, default 0.00)
  - jd_skills.importance                       (new enum jd_skill_importance_enum:
                                                 CORE, SUPPORTING)

This is not a guess: a real DETERMINISTIC_SCORE_COMPUTED audit_log row
sampled during the same investigation has this in its `detail` JSON -
'max_missing_core_skills': 3, 'required_skill_coverage_threshold': 0.0,
and per-skill 'importance': 'core'/'supporting' - the exact same concepts,
with values matching these columns' defaults. The scoring code already
computes these values today (hardcoded/config-driven); this schema was
very likely in-progress work to make them per-campaign/per-skill
configurable columns, applied directly to the shared dev DB via `alembic
upgrade`, with the migration file (and the corresponding model/service
wiring - HiringCampaign/JDSkill have no such fields today) never
committed, or reverted after the DB change had already landed.

Authored as a no-op merge into the exact stamped revision id - deliberately
does NOT attempt to recreate the original DDL (unknown: whether there was
an index, a check constraint, exact column ordering, etc. - only what's
observably true today), matching the same conservative methodology as
9a1c2f3e6b7d/b6dda6ad1824 above. If/when the corresponding model fields
and application logic are actually built, that work adds the code side
against a schema that already exists - it is not redundant with this
placeholder.

Revision ID: d3a86f21c9e4
Revises: 08655d0b0117
Create Date: 2026-08-11
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd3a86f21c9e4'
down_revision: Union[str, Sequence[str], None] = '08655d0b0117'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
