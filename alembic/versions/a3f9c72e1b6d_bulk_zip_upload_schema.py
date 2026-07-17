"""bulk zip upload: schema + audit enum support (M05-E02 Phase B0)

Adds the columns Epic 2 (Bulk ZIP Upload) needs to correlate individual
resumes/tasks back to their parent bulk_upload_job, plus the
consent_confirmed flag on bulk_upload_jobs, plus the new audit enum
values these stories reference. All three columns are nullable (or
default-valued) additions to existing tables — no backfill needed, and
NULL is the exactly-correct value for every row Epic 1 already created
(individual uploads never belonged to a bulk job).

Revision ID: a3f9c72e1b6d
Revises: d5c1a0b2e3f4
Create Date: 2026-07-16
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a3f9c72e1b6d"
down_revision = "d5c1a0b2e3f4"
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction block.
transactional_ddl = False


def upgrade() -> None:
    op.add_column(
        "resumes",
        sa.Column(
            "bulk_upload_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bulk_upload_jobs.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "celery_task_log",
        sa.Column(
            "bulk_upload_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bulk_upload_jobs.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "bulk_upload_jobs",
        sa.Column(
            "consent_confirmed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'BULK_UPLOAD_CANCELLED'")
    op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'BULK_UPLOAD_HISTORY_EXPORTED'")
    op.execute("ALTER TYPE audit_entity_type_enum ADD VALUE IF NOT EXISTS 'BULK_UPLOAD_JOB'")


def downgrade() -> None:
    op.drop_column("bulk_upload_jobs", "consent_confirmed")
    op.drop_column("celery_task_log", "bulk_upload_job_id")
    op.drop_column("resumes", "bulk_upload_job_id")
    # PostgreSQL cannot drop values from an enum type; leaving them in
    # place is harmless.
