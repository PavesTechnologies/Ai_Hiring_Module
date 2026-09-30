"""audit_action_type_enum: JD_SKILL_UPDATED, JD_SKILL_REMOVED

Audit actions for HR editing a JD's skill list (changing a skill's
mandatory/importance, or removing it from the JD).

Revision ID: 9c2f6e1a4d73
Revises: 5a7e3c9d1b24
Create Date: 2026-09-25
"""
from typing import Sequence, Union

from alembic import op

revision: str = "9c2f6e1a4d73"
down_revision: Union[str, Sequence[str], None] = "5a7e3c9d1b24"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ACTION_TYPES = ("JD_SKILL_UPDATED", "JD_SKILL_REMOVED")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for value in _ACTION_TYPES:
            op.execute(f"ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # PostgreSQL cannot drop an enum value; leaving it in place is harmless.
    pass
