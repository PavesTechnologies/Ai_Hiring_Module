"""celery_task_log dispatch_failed

Resume-upload resilience: distinguishes "apply_async was never able to
reach the broker" (dispatch_failed=True - safe to redispatch with the
same task_id, nothing is sitting in the queue yet) from "apply_async
succeeded, the message is already queued, just waiting for a worker"
(dispatch_failed=False - must NOT be redispatched, or the same task would
run twice since process_resume_document has no SUCCESS-shortcut). Without
this column, both states look identical from celery_task_log alone
(status=QUEUED, started_at IS NULL), which would make the recovery job
unsafe to write.

Revision ID: d2a7c9e4f1b6
Revises: ee6515ea0cf6
Create Date: 2026-08-04
"""
from alembic import op
import sqlalchemy as sa

revision = "d2a7c9e4f1b6"
down_revision = "ee6515ea0cf6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "celery_task_log",
        sa.Column("dispatch_failed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("celery_task_log", "dispatch_failed")
