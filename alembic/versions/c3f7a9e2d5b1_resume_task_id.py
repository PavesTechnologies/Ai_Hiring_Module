"""resume task_id (permanent resume -> task mapping)

Adds a nullable task_id column to resumes so a resume can be resolved to
its own Celery task at any point in its lifecycle, not just after that
task's first success. celery_task_log.resume_id is only ever populated
in process_resume_document's success path today - a resume still on its
first attempt, or one that fails before ever succeeding, leaves that
column NULL forever. This column is set once, at enqueue time in
ResumeIntakeService.upload_resume, before the task is dispatched.

Additive and nullable - no backfill, no impact on any existing row.

Revision ID: c3f7a9e2d5b1
Revises: a7c4e9f1d2b8
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa

revision = "c3f7a9e2d5b1"
down_revision = "a7c4e9f1d2b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "resumes",
        sa.Column("task_id", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("resumes", "task_id")
