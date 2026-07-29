"""add prompt_templates.name (mandatory)

Prompt Management (AIRS): adds a required human-readable name to
prompt_templates, backing CreatePromptRequest.name/UpdatePromptRequest.name.

Revision ID: c9e2a5f8b1d4
Revises: b2c8d4e1f7a3
Create Date: 2026-07-28
"""
from alembic import op
import sqlalchemy as sa

revision = "c9e2a5f8b1d4"
down_revision = "b2c8d4e1f7a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("prompt_templates", sa.Column("name", sa.String(length=150), nullable=True))

    # Backfill any pre-existing rows before tightening to NOT NULL - they
    # were created before this column existed.
    op.execute("UPDATE prompt_templates SET name = task_type WHERE name IS NULL")

    op.alter_column("prompt_templates", "name", nullable=False)


def downgrade() -> None:
    op.drop_column("prompt_templates", "name")
