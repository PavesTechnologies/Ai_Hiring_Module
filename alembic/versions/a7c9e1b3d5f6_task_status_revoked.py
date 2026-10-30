"""task_status_enum: REVOKED - a queued task revoked before it ran

celery_task_log had no status for a task revoked while still queued: its row stayed
QUEUED for good (the 34 duplicate score-only tasks queued and revoked on 2026-10-26).
Additive: a new enum value; no existing value changes.

ALTER TYPE ... ADD VALUE cannot run inside a transaction block (same precedent as
the audit-enum migrations, e.g. 09f831e39061).

Revision ID: a7c9e1b3d5f6
Revises: e3a5c7e9f1b2
Create Date: 2026-10-30
"""
from typing import Sequence, Union

from alembic import op

revision: str = "a7c9e1b3d5f6"
down_revision: Union[str, Sequence[str], None] = "e3a5c7e9f1b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

transactional_ddl = False


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE task_status_enum ADD VALUE IF NOT EXISTS 'REVOKED'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type; leaving it in place is harmless.
    pass
