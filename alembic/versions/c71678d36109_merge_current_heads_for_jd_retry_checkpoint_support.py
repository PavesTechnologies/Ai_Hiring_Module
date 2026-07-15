"""merge current heads for jd retry checkpoint support

Revision ID: c71678d36109
Revises: f3a6c9d1b7e2
Create Date: 2026-07-13 00:00:00.000000

The three revisions this originally merged (a92163422dba, 793ec14a7a28,
c8f2a4d6e910) were consolidated into 265912f5590a_initial_schema.py by an
earlier squash and no longer exist as files; this now chains directly onto
the real post-squash tip instead of dangling on deleted revisions.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c71678d36109'
down_revision: Union[str, Sequence[str], None] = 'f3a6c9d1b7e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
