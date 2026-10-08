"""ai_provider_model: per-purpose model assignments on a registered AI provider

A provider can now run extraction (JD + resume parsing) and AI evaluation on
different models, each with an optional backup model. Every existing
provider is backfilled with one BOTH assignment using its current
model_name, so processing behaves exactly as before until an admin splits
it.

Revision ID: d4b7e2a9c1f6
Revises: 9c2f6e1a4d73
Create Date: 2026-09-30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d4b7e2a9c1f6"
down_revision: Union[str, Sequence[str], None] = "9c2f6e1a4d73"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_provider_model",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "provider_config_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ai_provider_config.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("usage", sa.String(20), nullable=False),
        sa.Column("model_name", sa.String(200), nullable=False),
        sa.Column("fallback_model_name", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("provider_config_id", "usage", name="uq_ai_provider_model_usage"),
        sa.UniqueConstraint("provider_config_id", "model_name", name="uq_ai_provider_model_model"),
        sa.CheckConstraint("usage IN ('EXTRACTION', 'EVALUATION', 'BOTH')", name="ck_ai_provider_model_usage"),
        sa.CheckConstraint(
            "fallback_model_name IS NULL OR fallback_model_name <> model_name",
            name="ck_ai_provider_model_fallback_differs",
        ),
    )
    op.execute(
        """
        INSERT INTO ai_provider_model (id, provider_config_id, usage, model_name)
        SELECT gen_random_uuid(), id, 'BOTH', model_name FROM ai_provider_config
        """
    )


def downgrade() -> None:
    op.drop_table("ai_provider_model")
