"""add deterministic threshold to hiring campaigns

Revision ID: 83b9f848edcf
Revises: 2c82aaa93c9f
Create Date: 2026-07-17 17:14:40.253787

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '83b9f848edcf'
down_revision: Union[str, Sequence[str], None] = '2c82aaa93c9f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'hiring_campaigns',
        sa.Column('deterministic_threshold', sa.Numeric(5, 2), nullable=False, server_default='70.00'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('hiring_campaigns', 'deterministic_threshold')
