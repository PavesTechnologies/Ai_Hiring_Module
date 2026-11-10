"""audit enums: EMAIL_SEND_RESOLVED action and EMAIL_NOTIFICATION entity (stuck-email resolution)

Revision ID: b2d4f6a8c0e3
Revises: a1c3e5f7b9d2
Create Date: 2026-11-10

Asked for 2026-11-10: an audited HR_ADMIN action that resolves an email stuck in SENDING as sent or
failed. Additive: two enum values. Postgres cannot drop an enum value; downgrade leaves them, unused.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "b2d4f6a8c0e3"
down_revision: Union[str, None] = "a1c3e5f7b9d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'EMAIL_SEND_RESOLVED'")
        op.execute("ALTER TYPE audit_entity_type_enum ADD VALUE IF NOT EXISTS 'EMAIL_NOTIFICATION'")


def downgrade() -> None:
    pass  # PostgreSQL cannot drop a value from an enum type; leaving them is harmless.
