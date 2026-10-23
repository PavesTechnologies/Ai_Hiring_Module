"""LLM call log - every request to an LLM provider counts

- llm_call_log: one row per real request (main or backup model) from any
  caller - parsing, JD extraction, AI evaluation, skill confirmation, the
  Settings key check, scripts and dry runs - with provider, model, purpose,
  source, context (app / dry_run / ...), outcome, latency and tokens. The
  daily usage and the v2 daily cap are counted from it per the provider's
  quota day.

Revision ID: a4c6e8f0b2d5
Revises: f3b5d7f9a1c4
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a4c6e8f0b2d5"
down_revision: Union[str, Sequence[str], None] = "f3b5d7f9a1c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "llm_call_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("model", sa.String(120), nullable=False),
        sa.Column("purpose", sa.String(30), nullable=True),
        sa.Column("source", sa.String(60), nullable=True),
        sa.Column("context", sa.String(60), nullable=False, server_default="app"),
        sa.Column("outcome", sa.String(40), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
    )
    op.create_index("ix_llm_call_log_created_at", "llm_call_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_llm_call_log_created_at", table_name="llm_call_log")
    op.drop_table("llm_call_log")
