"""audit_action_type_enum: RESUME_DUPLICATE_DETECTED, RESUME_DUPLICATE_USE_EXISTING

Duplicate-file upload fix (separate from scoring v2): the duplicate warning
and the recruiter's "use existing" choice are now audited.

Revision ID: b5d7f9a1c3e6
Revises: a2c4e6f8b0d3
Create Date: 2026-10-08
"""
from typing import Sequence, Union

from alembic import op

revision: str = "b5d7f9a1c3e6"
down_revision: Union[str, Sequence[str], None] = "a2c4e6f8b0d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ACTION_TYPES = ("RESUME_DUPLICATE_DETECTED", "RESUME_DUPLICATE_USE_EXISTING")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for value in _ACTION_TYPES:
            op.execute(f"ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # Enum values can't be dropped in PostgreSQL; left in place.
    pass
