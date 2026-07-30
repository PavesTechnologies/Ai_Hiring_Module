"""add prompt_templates table

Prompt Management (AIRS): one prompt template per task_type
(AI_EVALUATE, JD_PARSE, RESUME_PARSE), backing
app/models/prompt_template.py (PromptTemplate).

Revision ID: a4b7c1d9e2f5
Revises: f4a8d2c6b9e1
Create Date: 2026-07-28
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a4b7c1d9e2f5"
down_revision = "f4a8d2c6b9e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prompt_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_type", sa.String(length=100), nullable=False),
        sa.Column("template_text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum("ACTIVE", "INACTIVE", name="prompt_template_status_enum"),
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], name=op.f("prompt_templates_updated_by_fkey")),
        sa.PrimaryKeyConstraint("id", name=op.f("prompt_templates_pkey")),
        sa.UniqueConstraint("task_type", name="uq_prompt_templates_task_type"),
    )
    op.create_index("idx_prompt_templates_status", "prompt_templates", ["status"])
    op.create_index("idx_prompt_templates_content_hash", "prompt_templates", ["content_hash"])
    op.create_index(
        op.f("ix_prompt_templates_task_type"), "prompt_templates", ["task_type"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_prompt_templates_task_type"), table_name="prompt_templates")
    op.drop_index("idx_prompt_templates_content_hash", table_name="prompt_templates")
    op.drop_index("idx_prompt_templates_status", table_name="prompt_templates")
    op.drop_table("prompt_templates")
    op.execute("DROP TYPE IF EXISTS prompt_template_status_enum")
