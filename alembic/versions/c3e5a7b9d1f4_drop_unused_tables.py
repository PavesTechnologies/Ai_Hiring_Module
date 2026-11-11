"""drop unused tables: prompt_versions, api_rate_limit_buckets; hiring_campaigns.report_scheduled

Revision ID: c3e5a7b9d1f4
Revises: b2d4f6a8c0e3
Create Date: 2026-11-11

Approved 2026-11-11 (pre-deployment dead-code audit): no code reads or writes these, and dev holds no
rows / no true values. campaign_candidate_ai_evaluations.prompt_version_id goes too: it is the foreign
key into prompt_versions, never written (0 non-null in dev), so the table cannot be dropped without it.
Downgrade recreates the empty structures exactly as the initial schema made them.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3e5a7b9d1f4"
down_revision: Union[str, None] = "b2d4f6a8c0e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("campaign_candidate_ai_evaluations_prompt_version_id_fkey",
                       "campaign_candidate_ai_evaluations", type_="foreignkey")
    op.drop_column("campaign_candidate_ai_evaluations", "prompt_version_id")
    op.drop_index("uq_prompt_versions_active_per_task", table_name="prompt_versions")
    op.drop_table("prompt_versions")
    op.drop_table("api_rate_limit_buckets")
    op.drop_column("hiring_campaigns", "report_scheduled")


def downgrade() -> None:
    op.add_column("hiring_campaigns", sa.Column("report_scheduled", sa.Boolean(), server_default=sa.text("false"),
                                                nullable=False))
    op.create_table(
        "api_rate_limit_buckets",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("service_name", sa.String(length=100), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_seconds", sa.Integer(), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("request_limit", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("service_name", "window_start"),
    )
    op.create_table(
        "prompt_versions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("task_type", sa.String(length=100), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("template_text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("model_name", sa.String(length=100), nullable=False),
        sa.Column("max_tokens", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_type", "content_hash"),
        sa.UniqueConstraint("task_type", "version_number"),
    )
    op.create_index("uq_prompt_versions_active_per_task", "prompt_versions", ["task_type"], unique=True,
                    postgresql_where=sa.text("is_active = TRUE"))
    op.add_column("campaign_candidate_ai_evaluations", sa.Column("prompt_version_id", sa.UUID(), nullable=True))
    op.create_foreign_key("campaign_candidate_ai_evaluations_prompt_version_id_fkey",
                          "campaign_candidate_ai_evaluations", "prompt_versions", ["prompt_version_id"], ["id"])
