"""audit_action_type_enum: CANDIDATE_RESCORE_REQUESTED (recruiter re-score of an out-of-date candidate)

Revision ID: f3b5d7a9c1e4
Revises: e9a4c6b2d8f1
Create Date: 2026-10-08
"""
from typing import Sequence, Union

from alembic import op

revision: str = "f3b5d7a9c1e4"
down_revision: Union[str, Sequence[str], None] = "e9a4c6b2d8f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'CANDIDATE_RESCORE_REQUESTED'")


def downgrade() -> None:
    # Enum values can't be dropped in PostgreSQL; left in place.
    pass
