"""audit_action_type_enum/audit_entity_type_enum add Prompt Management values

Adds the audit values AuditService.log() needs to record Prompt Management
CRUD activity: PROMPT_CREATED, PROMPT_UPDATED, PROMPT_DELETED,
PROMPT_STATUS_CHANGED (audit_action_type_enum) and PROMPT_TEMPLATE
(audit_entity_type_enum).

Revision ID: b2c8d4e1f7a3
Revises: a4b7c1d9e2f5
Create Date: 2026-07-28
"""
from alembic import op

revision = "b2c8d4e1f7a3"
down_revision = "a4b7c1d9e2f5"
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction block.
transactional_ddl = False


def upgrade() -> None:
    for value in (
        "PROMPT_CREATED",
        "PROMPT_UPDATED",
        "PROMPT_DELETED",
        "PROMPT_STATUS_CHANGED",
    ):
        op.execute(f"ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS '{value}'")

    op.execute("ALTER TYPE audit_entity_type_enum ADD VALUE IF NOT EXISTS 'PROMPT_TEMPLATE'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type; leaving these in place is harmless.
    pass
