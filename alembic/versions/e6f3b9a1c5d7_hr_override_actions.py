"""audit_action_type_enum add DETERMINISTIC_OVERRIDE_APPLIED and OVERRIDE_REPORT_EXPORTED

M07-E03 S04: adds the audit action values AuditService.log() needs to
record an HR_ADMIN override of a deterministic rejection, and an override
report export.

Revision ID: e6f3b9a1c5d7
Revises: d8a1e5b3c724
Create Date: 2026-07-24
"""
from alembic import op

revision = "e6f3b9a1c5d7"
down_revision = "d8a1e5b3c724"
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction block.
transactional_ddl = False


def upgrade() -> None:
    op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'DETERMINISTIC_OVERRIDE_APPLIED'")
    op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'OVERRIDE_REPORT_EXPORTED'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type; leaving it in place is harmless.
    pass
