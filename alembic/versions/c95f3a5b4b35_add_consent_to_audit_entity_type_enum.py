"""add consent to audit_entity_type_enum

Revision ID: c95f3a5b4b35
Revises: c4f8b2d6e1a3
Create Date: 2026-08-04 12:50:06.347749
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c95f3a5b4b35"
down_revision: Union[str, Sequence[str], None] = "c4f8b2d6e1a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TYPE audit_entity_type_enum
        ADD VALUE IF NOT EXISTS 'CONSENT';
    """)


def downgrade() -> None:
    # PostgreSQL doesn't support removing enum values safely.
    pass