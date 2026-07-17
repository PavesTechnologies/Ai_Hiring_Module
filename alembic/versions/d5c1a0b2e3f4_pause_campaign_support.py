"""pause campaign support: TaskStatus.PAUSED

Adds the PAUSED value to task_status_enum so QUEUED Celery tasks can be
soft-cancelled when a campaign is paused (M04-E03 S01-T02).

CAMPAIGN_PAUSED already exists in audit_action_type_enum, so no audit DDL.

Revision ID: d5c1a0b2e3f4
Revises: 265912f5590a
Create Date: 2026-07-13

NOTE (2026-07-16): down_revision was originally recorded as
"c8f2a4d6e910" — a revision ID that does not exist anywhere in this
versions folder, breaking the whole chain (alembic history / upgrade /
stamp all failed with KeyError). Verified directly against the live
database that this migration's own upgrade() had already been applied
(task_status_enum already had PAUSED, audit_action_type_enum already had
CAMPAIGN_RESUMED) — only the chain bookkeeping was broken, not the
schema. Corrected down_revision to point at the real prior migration
(265912f5590a) and stamped the database to this revision to reconcile
alembic_version (which had drifted to a third, also-nonexistent value,
"e7a4f2c9d8b1") with reality, without re-running any DDL.
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
