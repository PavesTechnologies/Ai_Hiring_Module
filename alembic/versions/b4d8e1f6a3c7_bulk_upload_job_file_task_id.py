"""bulk upload job file task_id (M05-E02 tracking follow-up)

Adds a nullable task_id column to bulk_upload_job_files so a specific
file row can be correlated back to its own Celery task in
celery_task_log — needed to surface retry-attempt counts per file on the
bulk upload detail endpoint. Previously there was no link from a file row
to its task at all; celery_task_log only carries bulk_upload_job_id,
shared across every file in the job, which isn't specific enough.

Additive and nullable — no backfill, no impact on any existing row.

Revision ID: b4d8e1f6a3c7
Revises: f2c9b8e4a1d3
Create Date: 2026-07-21
"""
from alembic import op
import sqlalchemy as sa

revision = "b4d8e1f6a3c7"
down_revision = "f2c9b8e4a1d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bulk_upload_job_files",
        sa.Column("task_id", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("bulk_upload_job_files", "task_id")
