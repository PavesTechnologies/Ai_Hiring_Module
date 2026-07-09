"""add jd_exported audit action

Revision ID: 06b5ad918767
Revises: 061f75a12958
Create Date: 2026-07-06 19:01:50.860583

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '06b5ad918767'
down_revision: Union[str, Sequence[str], None] = '061f75a12958'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TYPE audit_action_type_enum
        ADD VALUE IF NOT EXISTS 'JD_EXPORTED';
    """)


def downgrade() -> None:
    """Downgrade schema."""
    pass
