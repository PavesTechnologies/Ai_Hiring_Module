"""audit enum skill ontology values

Adds the audit_action_type_enum / audit_entity_type_enum values the
skill-ontology work introduced on the model side (JD_REPROCESSED,
UNKNOWN_SKILL_CREATED/MAPPED/PROMOTED/DISMISSED, JD_SKILL_REMAPPED,
ALIAS_ADDED, JD_SKILL, UNKNOWN_SKILL, SKILL_ONTOLOGY) - present in
AuditActionType/AuditEntityType but never applied to the DB enum types
(same "DB stamped past the revision whose DDL never ran" drift found and
fixed for jd_skills/jd_unknown_skills/unknown_skills in d88f9123b149).

Revision ID: f3a6c9d1b7e2
Revises: d88f9123b149
Create Date: 2026-07-14
"""
from alembic import op

revision = "f3a6c9d1b7e2"
down_revision = "d88f9123b149"
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction block.
transactional_ddl = False


def upgrade() -> None:
    for value in (
        "JD_REPROCESSED",
        "UNKNOWN_SKILL_CREATED",
        "UNKNOWN_SKILL_MAPPED",
        "UNKNOWN_SKILL_PROMOTED",
        "UNKNOWN_SKILL_DISMISSED",
        "JD_SKILL_REMAPPED",
        "ALIAS_ADDED",
    ):
        op.execute(f"ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS '{value}'")

    for value in ("JD_SKILL", "UNKNOWN_SKILL", "SKILL_ONTOLOGY"):
        op.execute(f"ALTER TYPE audit_entity_type_enum ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type; leaving these in
    # place is harmless.
    pass
