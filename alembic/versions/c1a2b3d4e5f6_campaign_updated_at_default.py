"""campaign updated_at default

Revision ID: c1a2b3d4e5f6
Revises: ebea24c6e01a
Create Date: 2026-07-02 17:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c1a2b3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'ebea24c6e01a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column(
        'hiring_campaigns',
        'updated_at',
        existing_type=sa.DateTime(timezone=True),
        server_default=sa.text('now()'),
        existing_nullable=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column(
        'hiring_campaigns',
        'updated_at',
        existing_type=sa.DateTime(timezone=True),
        server_default=None,
        existing_nullable=False,
    )
