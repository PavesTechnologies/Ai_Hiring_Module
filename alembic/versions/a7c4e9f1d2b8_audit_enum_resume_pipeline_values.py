"""audit enum resume pipeline values

Adds the audit_action_type_enum / audit_entity_type_enum values the new
Resume processing pipeline introduced (RESUME_PARSED, RESUME_PARSE_FAILED,
CANDIDATE_SKILL_MATCHED, CANDIDATE_SKILL) so ResumeService's audit_service.log()
calls don't fail at runtime with an invalid-enum-value error. RESUME and
RESUME_UPLOADED were already present on the live DB from an undocumented
prior ALTER TYPE run (confirmed via direct query before writing this
migration) - they are re-added here with IF NOT EXISTS so this migration
stays reproducible from a clean database too, matching the intent of
f3a6c9d1b7e2's fix for the same class of drift.

ALTER TYPE ... ADD VALUE cannot run inside a transaction block in the
Postgres versions this project has hit that error against before (see
f3a6c9d1b7e2) - transactional_ddl = False below, same as that migration.

Revision ID: a7c4e9f1d2b8
Revises: b6e2d9a41c3f
Create Date: 2026-07-17
"""
from alembic import op

revision = "a7c4e9f1d2b8"
down_revision = "b6e2d9a41c3f"
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction block.
transactional_ddl = False


def upgrade() -> None:
    for value in (
        "RESUME_UPLOADED",
        "RESUME_PARSED",
        "RESUME_PARSE_FAILED",
        "CANDIDATE_SKILL_MATCHED",
    ):
        op.execute(f"ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS '{value}'")

    for value in ("RESUME", "CANDIDATE_SKILL"):
        op.execute(f"ALTER TYPE audit_entity_type_enum ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type; leaving these in
    # place is harmless.
    pass
