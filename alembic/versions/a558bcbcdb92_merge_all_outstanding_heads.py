"""merge all outstanding heads

Pure bookkeeping merge, no DDL: `alembic heads` originally reported 8
independent heads (3ae81a38abdc, 3e7800c51995, 90b05f9f2aa1, b4d8e1f6a3c7,
c2e6a1f8d4b7, d4b7f1c8a3e6, d6b8e3a1f4c9, f5b8d2c4a917). This is the same
multi-head condition documented in docs/resume_intake_implementation_log.md
("the Alembic chain has been multi-headed and broken since Epic 2") and
explicitly called out in b1f4c9a2e7d3's docstring ("this repo currently has
multiple concurrent Alembic heads ... that merge is a separate, pre-existing
concern outside this epic's scope").

UPDATE after direct DB inspection: the live database's alembic_version was
found stamped at 'c95f3a5b4b35', a revision absent from this repo and its
entire git history - so `alembic current`/`alembic upgrade head` failed with
"Can't locate revision identified by 'c95f3a5b4b35'" before ever reaching
this merge. c95f3a5b4b35 was added as a placeholder revision (see that
file for the per-object verification) descending from exactly the 7 of
these 8 heads whose DDL was confirmed present in the live DB - every one
except c2e6a1f8d4b7, whose composite-scoring/campaign-weight-history branch
was confirmed entirely absent. This revision's down_revision was therefore
changed from the raw 8-head tuple to (c95f3a5b4b35, c2e6a1f8d4b7): the
verified-current real-world position, merged with the one branch still
outstanding.

This revision performs no DDL of its own. The actual schema repair (adding
the missing column/tables/enum values, guarded so it is a no-op wherever
they already exist) is done by the following revision, ee6515ea0cf6,
exactly as d88f9123b149 separated "sync schema drift" from its own merge
history.

Revision ID: a558bcbcdb92
Revises: c95f3a5b4b35, c2e6a1f8d4b7
Create Date: 2026-08-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a558bcbcdb92'
down_revision: Union[str, Sequence[str], None] = ('c95f3a5b4b35', 'c2e6a1f8d4b7')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
