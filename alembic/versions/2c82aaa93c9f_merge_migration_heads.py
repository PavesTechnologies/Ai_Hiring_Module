"""merge migration heads

Revision ID: 2c82aaa93c9f
Revises: 4fcdbc589048, e7a4f2c9d8b1
Create Date: 2026-07-17 17:13:13.455493

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2c82aaa93c9f'
down_revision: Union[str, Sequence[str], None] = ('4fcdbc589048', 'e7a4f2c9d8b1')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
