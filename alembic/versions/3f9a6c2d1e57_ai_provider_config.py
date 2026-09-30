"""ai_provider_config

Settings -> AI model: the admin-selected LLM provider (Google Gemini /
Anthropic / OpenAI / Groq), model and encrypted API key used by every AI
step instead of the hard-coded .env Gemini config (which stays as the
fallback when no verified row exists). At most one active row, enforced
by a partial unique index.

Also adds the audit enum values its service logs, and doubles as the merge
of the two heads that existed when it was written (0a55d70d8d55 from the
governance-model chain and c4d8e21a9f6b from the interview chain).

ALTER TYPE ... ADD VALUE runs inside autocommit_block() - env.py doesn't
read a module-level transactional_ddl flag, so this is what actually
keeps it out of the migration transaction.

Revision ID: 3f9a6c2d1e57
Revises: 0a55d70d8d55, c4d8e21a9f6b
Create Date: 2026-09-23
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3f9a6c2d1e57"
down_revision: Union[str, Sequence[str], None] = ("0a55d70d8d55", "c4d8e21a9f6b")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ACTION_TYPES = ("AI_PROVIDER_CONFIG_UPDATED", "AI_PROVIDER_CONFIG_DELETED")
_ENTITY_TYPES = ("AI_PROVIDER_CONFIG",)


def upgrade() -> None:
    op.create_table(
        "ai_provider_config",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("model_name", sa.String(200), nullable=False),
        sa.Column("api_key_encrypted", postgresql.BYTEA(), nullable=False),
        sa.Column(
            "encryption_key_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("encryption_keys.id"), nullable=False,
        ),
        sa.Column("api_key_last4", sa.String(8), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.String(255), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "uq_ai_provider_config_single_active",
        "ai_provider_config",
        ["is_active"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )

    with op.get_context().autocommit_block():
        for value in _ACTION_TYPES:
            op.execute(f"ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS '{value}'")
        for value in _ENTITY_TYPES:
            op.execute(f"ALTER TYPE audit_entity_type_enum ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    op.drop_index("uq_ai_provider_config_single_active", table_name="ai_provider_config")
    op.drop_table("ai_provider_config")
    # PostgreSQL cannot drop a value from an enum type; leaving the audit
    # enum values in place is harmless.
