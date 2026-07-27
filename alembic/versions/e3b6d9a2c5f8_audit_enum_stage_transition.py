"""audit enum stage transition value (Epic 3 / M05-E03 Phase C0)

Adds the audit_action_type_enum value PipelineTransitionService needs to
log a validated pipeline_stage move, so AuditService.log() calls with it
don't fail at runtime with an invalid-enum-value error.

ALTER TYPE ... ADD VALUE cannot run inside a transaction block in the
Postgres versions this project has hit that error against before (see
f3a6c9d1b7e2) - transactional_ddl = False below, same as that migration.

Revision ID: e3b6d9a2c5f8
Revises: d8a2f5c1b4e7
Create Date: 2026-07-24
"""
from alembic import op

revision = "e3b6d9a2c5f8"
down_revision = "d8a2f5c1b4e7"
branch_labels = None
depends_on = None

transactional_ddl = False


def upgrade() -> None:
    op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'PIPELINE_STAGE_TRANSITIONED'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type; leaving it in place is harmless.
    pass
