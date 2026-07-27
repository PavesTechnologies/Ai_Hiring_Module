"""unknown skill deleted audit action

Adds UNKNOWN_SKILL_DELETED to audit_action_type_enum for the Unknown Skill
hard-delete endpoint (DELETE /skills/unknown/{unknown_skill_id}).

Revision ID: b86c7b089b02
Revises: b23d4281c230
Create Date: 2026-07-24
"""
from alembic import op

revision = "b86c7b089b02"
down_revision = "b23d4281c230"
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction block.
transactional_ddl = False


def upgrade() -> None:
    op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'UNKNOWN_SKILL_DELETED'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type; leaving the new
    # value in place is harmless.
    pass
