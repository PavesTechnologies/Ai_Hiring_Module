"""skill updated audit action

Adds SKILL_UPDATED to audit_action_type_enum and SKILL to
audit_entity_type_enum so Skill Ontology edits (S03-T01) can be audit
logged through the existing AuditService, matching the JD_EXPORTED /
CAMPAIGN_RESUMED precedent for extending these enums.

Revision ID: 26701516349d
Revises: d5c1a0b2e3f4
Create Date: 2026-07-14
"""
from alembic import op

revision = "26701516349d"
down_revision = "d5c1a0b2e3f4"
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction block.
transactional_ddl = False


def upgrade() -> None:
    op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'SKILL_UPDATED'")
    op.execute("ALTER TYPE audit_entity_type_enum ADD VALUE IF NOT EXISTS 'SKILL'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type; leaving the new
    # values in place is harmless.
    pass
