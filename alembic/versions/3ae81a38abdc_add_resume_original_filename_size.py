"""add resume original_filename and file_size_bytes

Revision ID: 3ae81a38abdc
Revises: a1c6d9f3e7b2
Create Date: 2026-07-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3ae81a38abdc'
down_revision: Union[str, Sequence[str], None] = 'a1c6d9f3e7b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('resumes', sa.Column('original_filename', sa.String(length=500), nullable=True))
    op.add_column('resumes', sa.Column('file_size_bytes', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('resumes', 'file_size_bytes')
    op.drop_column('resumes', 'original_filename')
