"""bulk upload cancellation (M05-E02 Phase B7)

Adds CANCELLED to both bulk_upload_status_enum and
bulk_upload_file_status_enum so a bulk upload job can be cancelled
mid-flight: the job itself moves to CANCELLED, and any still-queued
per-file rows are bulk-marked CANCELLED rather than being parsed.

Also adds RUNNING to bulk_upload_file_status_enum: the per-file parse
task atomically claims QUEUED -> RUNNING before doing any real work, so
cancel's bulk QUEUED -> CANCELLED update can never race with (and get
silently overwritten by) a file that's actually already being processed —
mirroring how CeleryTaskLog's own RUNNING status already keeps campaign
pause from touching in-flight work.

Revision ID: f2c9b8e4a1d3
Revises: e1b7c4a9d2f6
Create Date: 2026-07-18
"""
from alembic import op

revision = "f2c9b8e4a1d3"
down_revision = "e1b7c4a9d2f6"
branch_labels = None
depends_on = None

transactional_ddl = False


def upgrade() -> None:
    op.execute("ALTER TYPE bulk_upload_status_enum ADD VALUE IF NOT EXISTS 'CANCELLED'")
    op.execute("ALTER TYPE bulk_upload_file_status_enum ADD VALUE IF NOT EXISTS 'CANCELLED'")
    op.execute("ALTER TYPE bulk_upload_file_status_enum ADD VALUE IF NOT EXISTS 'RUNNING'")


def downgrade() -> None:
    # PostgreSQL cannot drop values from an enum type; leaving them in
    # place is harmless.
    pass
