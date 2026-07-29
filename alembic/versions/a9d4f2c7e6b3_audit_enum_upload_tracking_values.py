"""audit enum upload tracking values (Epic 4 / M05-E04 Phase D0)

Adds the 4 audit_action_type_enum values Epic 4's later phases need
(UPLOAD_HISTORY_EXPORTED - D8, RESUME_UPLOAD_RETRIED /
INDIVIDUAL_UPLOAD_DLQ_REPLAYED - D10, PLATFORM_ALERT_SENT - D13), so
AuditService.log() calls with any of them don't fail at runtime with an
invalid-enum-value error once those phases are built.

ALTER TYPE ... ADD VALUE cannot run inside a transaction block in the
Postgres versions this project has hit that error against before (see
f3a6c9d1b7e2) - transactional_ddl = False below, same as that migration.

Revision ID: a9d4f2c7e6b3
Revises: f7c1a4d8b3e6
Create Date: 2026-07-29
"""
from alembic import op

revision = "a9d4f2c7e6b3"
down_revision = "f7c1a4d8b3e6"
branch_labels = None
depends_on = None

transactional_ddl = False


def upgrade() -> None:
    op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'UPLOAD_HISTORY_EXPORTED'")
    op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'RESUME_UPLOAD_RETRIED'")
    op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'INDIVIDUAL_UPLOAD_DLQ_REPLAYED'")
    op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'PLATFORM_ALERT_SENT'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type; leaving it in place is harmless.
    pass
