"""Duplicate resume decisions - upload_decisions, bulk file statuses, audit values

- upload_decisions: a bulk file whose candidate is already in the campaign
  with a different file, held (file kept in storage, nothing else changed)
  until the uploader or an HR admin chooses "keep existing" or "use new
  version"; PENDING rows expire to EXPIRED_KEPT_EXISTING after 30 days.
- bulk_upload_jobs.needs_decision_count (default 0).
- bulk_upload_file_status_enum: SKIPPED_DUPLICATE, NEEDS_DECISION.
- audit: RESUME_DUPLICATE_SKIPPED, RESUME_KEPT_EXISTING,
  RESUME_NEW_VERSION_USED, UPLOAD_DECISION_EXPIRED; entity UPLOAD_DECISION.

Revision ID: d6f8a0c2e4b7
Revises: a4c6e8f0b2d5
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d6f8a0c2e4b7"
down_revision: Union[str, Sequence[str], None] = "a4c6e8f0b2d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "upload_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("hiring_campaigns.id"), nullable=False),
        sa.Column("campaign_candidate_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("campaign_candidates.id", ondelete="SET NULL"), nullable=True),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("candidates.id", ondelete="SET NULL"), nullable=True),
        sa.Column("existing_resume_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("resumes.id", ondelete="SET NULL"), nullable=True),
        sa.Column("bulk_upload_job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("bulk_upload_jobs.id"),
                  nullable=True),
        sa.Column("bulk_upload_job_file_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("bulk_upload_job_files.id"), nullable=True),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("original_filename", sa.String(500), nullable=True),
        sa.Column("file_hash", sa.String(64), nullable=False),
        sa.Column("file_format", sa.String(10), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), nullable=True),
        sa.Column("page_count", sa.SmallInteger(), nullable=True),
        sa.Column("uploaded_by", sa.String(255), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="PENDING"),
        sa.Column("decided_by", sa.String(255), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("new_resume_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("resumes.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING', 'KEPT_EXISTING', 'NEW_VERSION_USED', 'EXPIRED_KEPT_EXISTING')",
            name="ck_upload_decisions_status",
        ),
    )
    op.create_index("ix_upload_decisions_campaign_status", "upload_decisions", ["campaign_id", "status"])
    op.create_index("ix_upload_decisions_bulk_upload_job_id", "upload_decisions", ["bulk_upload_job_id"])
    op.create_index("ix_upload_decisions_status_expires_at", "upload_decisions", ["status", "expires_at"])

    op.add_column(
        "bulk_upload_jobs",
        sa.Column("needs_decision_count", sa.Integer(), nullable=False, server_default="0"),
    )

    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE bulk_upload_file_status_enum ADD VALUE IF NOT EXISTS 'SKIPPED_DUPLICATE'")
        op.execute("ALTER TYPE bulk_upload_file_status_enum ADD VALUE IF NOT EXISTS 'NEEDS_DECISION'")
        op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'RESUME_DUPLICATE_SKIPPED'")
        op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'RESUME_KEPT_EXISTING'")
        op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'RESUME_NEW_VERSION_USED'")
        op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'UPLOAD_DECISION_EXPIRED'")
        op.execute("ALTER TYPE audit_entity_type_enum ADD VALUE IF NOT EXISTS 'UPLOAD_DECISION'")


def downgrade() -> None:
    # Enum values stay (Postgres can't drop them; unused values are harmless).
    op.drop_column("bulk_upload_jobs", "needs_decision_count")
    op.drop_index("ix_upload_decisions_status_expires_at", table_name="upload_decisions")
    op.drop_index("ix_upload_decisions_bulk_upload_job_id", table_name="upload_decisions")
    op.drop_index("ix_upload_decisions_campaign_status", table_name="upload_decisions")
    op.drop_table("upload_decisions")
