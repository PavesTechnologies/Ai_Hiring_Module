"""skill parent updated audit action

Adds SKILL_PARENT_UPDATED to audit_action_type_enum so parent-skill
reassignments (S05-T01) get their own dedicated audit action, distinct
from the general SKILL_UPDATED used for canonical_name/category/
confidence/aliases edits.

Revision ID: 9c5cde45f4bf
Revises: 2d2a6dfa9858
Create Date: 2026-07-15
"""
from alembic import op

revision = "9c5cde45f4bf"
down_revision = "2d2a6dfa9858"
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction block.
transactional_ddl = False


def upgrade() -> None:
    op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'SKILL_PARENT_UPDATED'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type; leaving the new
    # value in place is harmless.
    pass
