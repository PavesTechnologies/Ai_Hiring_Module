"""placeholder for missing revision

Revision ID: 265912f5590a
Revises: a41e892f4a72
Create Date: 2026-07-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '265912f5590a'
down_revision: Union[str, Sequence[str], None] = 'a41e892f4a72'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
