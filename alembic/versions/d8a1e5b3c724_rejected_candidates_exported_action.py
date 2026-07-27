"""audit_action_type_enum add REJECTED_CANDIDATES_EXPORTED

M07-E03 S03 T03: adds the audit action value AuditService.log() needs to
record a rejected-candidates export.

Revision ID: d8a1e5b3c724
Revises: c2f6a8d4e913
Create Date: 2026-07-25
"""
from alembic import op

revision = "d8a1e5b3c724"
down_revision = "c2f6a8d4e913"
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction block.
transactional_ddl = False


def upgrade() -> None:
    op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'REJECTED_CANDIDATES_EXPORTED'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type; leaving it in place is harmless.
    pass
