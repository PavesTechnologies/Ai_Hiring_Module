"""merge heads

Revision ID: 061f75a12958
Revises: c1a2b3d4e5f6, c7d8e9f0a1b2
Create Date: 2026-07-06 19:00:43.955208

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '061f75a12958'
down_revision: Union[str, Sequence[str], None] = ('c1a2b3d4e5f6', 'c7d8e9f0a1b2')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
