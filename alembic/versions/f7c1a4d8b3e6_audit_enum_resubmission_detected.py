"""audit enum resubmission detected value (Epic 3 / M05-E03 Phase C4)

Adds the audit_action_type_enum value ResubmissionAlertService needs to
log a high-frequency cross-campaign resubmission alert.

ALTER TYPE ... ADD VALUE cannot run inside a transaction block in the
Postgres versions this project has hit that error against before (see
f3a6c9d1b7e2) - transactional_ddl = False below, same as that migration.

Revision ID: f7c1a4d8b3e6
Revises: e3b6d9a2c5f8
Create Date: 2026-07-28
"""
from alembic import op

revision = "f7c1a4d8b3e6"
down_revision = "e3b6d9a2c5f8"
branch_labels = None
depends_on = None

transactional_ddl = False


def upgrade() -> None:
    op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'CAMPAIGN_RESUBMISSION_DETECTED'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type; leaving it in place is harmless.
    pass
