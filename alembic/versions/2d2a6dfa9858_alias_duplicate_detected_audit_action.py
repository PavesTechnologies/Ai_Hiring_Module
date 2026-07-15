"""alias duplicate detected audit action

Adds ALIAS_DUPLICATE_DETECTED to audit_action_type_enum so the scheduled
skill.detect_duplicate_aliases Celery Beat task (S04-T03) can audit-log
each duplicate alias it finds, reusing the existing EntityType.SKILL (no
new entity type needed).

Revision ID: 2d2a6dfa9858
Revises: 26701516349d
Create Date: 2026-07-14
"""
from alembic import op

revision = "2d2a6dfa9858"
down_revision = "26701516349d"
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction block.
transactional_ddl = False


def upgrade() -> None:
    op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'ALIAS_DUPLICATE_DETECTED'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type; leaving the new
    # value in place is harmless.
    pass
