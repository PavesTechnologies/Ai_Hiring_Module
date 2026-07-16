"""celery task log created_by + title

Revision ID: b6e2d9a41c3f
Revises: 4fd0a3c4f90d
Create Date: 2026-07-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b6e2d9a41c3f'
down_revision: Union[str, Sequence[str], None] = '4fd0a3c4f90d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('celery_task_log', sa.Column('created_by', sa.String(length=255), nullable=True))
    op.add_column('celery_task_log', sa.Column('title', sa.String(length=255), nullable=True))
    op.create_foreign_key(
        'celery_task_log_created_by_fkey',
        'celery_task_log', 'users',
        ['created_by'], ['id'],
    )


def downgrade() -> None:
    op.drop_constraint('celery_task_log_created_by_fkey', 'celery_task_log', type_='foreignkey')
    op.drop_column('celery_task_log', 'title')
    op.drop_column('celery_task_log', 'created_by')
