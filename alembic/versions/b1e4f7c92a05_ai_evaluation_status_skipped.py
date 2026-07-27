"""ai_evaluation_status_enum add SKIPPED value

M07-E03 S01 T03: a candidate rejected at the DETERMINISTIC layer must be
able to record campaign_candidates.ai_evaluation_status = SKIPPED when its
queued AI_EVALUATE task is cancelled. Adds the missing enum value only -
no other schema change.

ALTER TYPE ... ADD VALUE cannot run inside a transaction block in the
Postgres versions this project has hit that error against before (see
f3a6c9d1b7e2 / a7c4e9f1d2b8) - transactional_ddl = False, same as those.

Revision ID: b1e4f7c92a05
Revises: 44c9d277085e
Create Date: 2026-07-23
"""
from alembic import op

revision = "b1e4f7c92a05"
down_revision = "44c9d277085e"
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction block.
transactional_ddl = False


def upgrade() -> None:
    op.execute("ALTER TYPE ai_evaluation_status_enum ADD VALUE IF NOT EXISTS 'SKIPPED'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type; leaving it in place is harmless.
    pass
