"""bulk upload zip storage path (M05-E02 Phase B2)

Adds bulk_upload_jobs.zip_storage_path so a BULK_EXTRACT task that crashes
or is lost mid-flight can be identified and manually re-triggered later
purely from the database — the same recovery pattern already used once in
Epic 1 after a Windows Celery worker crash left two tasks stuck in Redis's
unacknowledged state.

Revision ID: c8d4f1a6e9b2
Revises: a3f9c72e1b6d
Create Date: 2026-07-16
"""
from alembic import op
import sqlalchemy as sa

revision = "c8d4f1a6e9b2"
down_revision = "a3f9c72e1b6d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bulk_upload_jobs",
        sa.Column("zip_storage_path", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("bulk_upload_jobs", "zip_storage_path")
