"""audit_action_type_enum add DETERMINISTIC_ANALYTICS_EXPORTED

M07-E03 S05 T03: adds the audit action value AuditService.log() needs to
record a platform-wide deterministic rejection analytics export.

Revision ID: f4a8d2c6b9e1
Revises: e6f3b9a1c5d7
Create Date: 2026-07-24
"""
from alembic import op

revision = "f4a8d2c6b9e1"
down_revision = "e6f3b9a1c5d7"
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction block.
transactional_ddl = False


def upgrade() -> None:
    op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'DETERMINISTIC_ANALYTICS_EXPORTED'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type; leaving it in place is harmless.
    pass
