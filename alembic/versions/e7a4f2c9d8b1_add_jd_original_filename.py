"""add jd original_filename

Revision ID: e7a4f2c9d8b1
Revises: b6e2d9a41c3f
Create Date: 2026-07-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7a4f2c9d8b1'
down_revision: Union[str, Sequence[str], None] = 'b6e2d9a41c3f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('job_descriptions', sa.Column('original_filename', sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column('job_descriptions', 'original_filename')
