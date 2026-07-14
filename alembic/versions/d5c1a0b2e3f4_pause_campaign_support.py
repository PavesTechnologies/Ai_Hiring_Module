"""pause campaign support: TaskStatus.PAUSED

Adds the PAUSED value to task_status_enum so QUEUED Celery tasks can be
soft-cancelled when a campaign is paused (M04-E03 S01-T02).

CAMPAIGN_PAUSED already exists in audit_action_type_enum, so no audit DDL.

Revision ID: d5c1a0b2e3f4
Revises: 265912f5590a
Create Date: 2026-07-13
"""
from alembic import op

revision = "d5c1a0b2e3f4"
down_revision = "265912f5590a"
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction block.
transactional_ddl = False


def upgrade() -> None:
    op.execute("ALTER TYPE task_status_enum ADD VALUE IF NOT EXISTS 'PAUSED'")
    op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'CAMPAIGN_RESUMED'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type; leaving 'PAUSED'
    # in place is harmless.
    pass
