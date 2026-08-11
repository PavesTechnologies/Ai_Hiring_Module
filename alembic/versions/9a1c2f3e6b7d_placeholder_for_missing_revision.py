"""placeholder for missing revision (merge)

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

Revision ID: 9a1c2f3e6b7d
Revises: 09f831e39061, e686c750b7b4
Create Date: 2026-08-10
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '9a1c2f3e6b7d'
down_revision: Union[str, Sequence[str], None] = ('09f831e39061', 'e686c750b7b4')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
