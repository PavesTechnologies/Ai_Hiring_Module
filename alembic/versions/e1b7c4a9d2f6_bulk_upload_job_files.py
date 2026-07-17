"""bulk upload job files (M05-E02 Phase B3)

New table staging each individual file extracted from a bulk-upload ZIP,
one row per file, until Phase B4's per-file parse task consumes it. Needed
because no Resume row can exist per file yet at extraction time (the
parse-first design only creates Resume/Candidate rows after a file's AI
extraction succeeds).

Revision ID: e1b7c4a9d2f6
Revises: c8d4f1a6e9b2
Create Date: 2026-07-16
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "e1b7c4a9d2f6"
down_revision = "c8d4f1a6e9b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bulk_upload_file_status_enum = postgresql.ENUM(
        "QUEUED", "PROCESSED", "FAILED",
        name="bulk_upload_file_status_enum",
    )

    op.create_table(
        "bulk_upload_job_files",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "bulk_upload_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bulk_upload_jobs.id"),
            nullable=False,
        ),
        sa.Column("original_filename", sa.String(500), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("status", bulk_upload_file_status_enum, nullable=False, server_default="QUEUED"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("bulk_upload_job_files")
    postgresql.ENUM(name="bulk_upload_file_status_enum").drop(op.get_bind())
