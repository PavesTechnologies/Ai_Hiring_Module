"""ai_provider_config: multiple providers, one row per provider

Settings now registers several AI providers (at most one row per
provider) and marks exactly one as active for processing. Previously the
table held a single "current" row, and a reset left inactive rows behind -
so before the unique constraint is added, each provider keeps its active
row (or, failing that, its most recently updated one) and the rest are
deleted.

The partial unique index on is_active from 3f9a6c2d1e57 is kept: at most
one active row.

Revision ID: 8b1d4e6f2a90
Revises: 3f9a6c2d1e57
Create Date: 2026-09-23
"""
from typing import Sequence, Union

from alembic import op

revision: str = "8b1d4e6f2a90"
down_revision: Union[str, Sequence[str], None] = "3f9a6c2d1e57"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ACTION_TYPES = ("AI_PROVIDER_CONFIG_CREATED", "AI_PROVIDER_CONFIG_ACTIVATED")


def upgrade() -> None:
    op.execute("""
        DELETE FROM ai_provider_config
        WHERE id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY provider
                    ORDER BY is_active DESC, updated_at DESC
                ) AS rn
                FROM ai_provider_config
            ) ranked
            WHERE ranked.rn > 1
        )
    """)
    op.create_unique_constraint("uq_ai_provider_config_provider", "ai_provider_config", ["provider"])

    with op.get_context().autocommit_block():
        for value in _ACTION_TYPES:
            op.execute(f"ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    op.drop_constraint("uq_ai_provider_config_provider", "ai_provider_config", type_="unique")
    # Deleted duplicate rows can't be restored, and PostgreSQL cannot drop an
    # enum value; leaving the audit enum values in place is harmless.
