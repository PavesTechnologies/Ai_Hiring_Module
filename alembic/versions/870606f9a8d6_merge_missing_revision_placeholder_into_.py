"""merge missing revision placeholder into current head

Revision ID: 870606f9a8d6
Revises: 265912f5590a, 4fd0a3c4f90d
Create Date: 2026-07-13 19:38:35.524530

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '870606f9a8d6'
down_revision: Union[str, Sequence[str], None] = ('265912f5590a', '4fd0a3c4f90d')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
