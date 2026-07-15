"""skill deactivated/reactivated audit actions

Adds SKILL_DEACTIVATED and SKILL_REACTIVATED to audit_action_type_enum
for S06 (Skill Deactivation & Reactivation).

Revision ID: 4fcdbc589048
Revises: 9c5cde45f4bf
Create Date: 2026-07-15
"""
from alembic import op

revision = "4fcdbc589048"
down_revision = "9c5cde45f4bf"
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction block.
transactional_ddl = False


def upgrade() -> None:
    op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'SKILL_DEACTIVATED'")
    op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'SKILL_REACTIVATED'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type; leaving the new
    # values in place is harmless.
    pass
