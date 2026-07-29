"""email trigger enum upload permanently failed value (Epic 4 / M05-E04 Phase D0)

Adds the email_trigger_event_enum value D11 needs to queue an
UPLOAD_PERMANENTLY_FAILED notification, so EmailNotification rows with it
don't fail at runtime with an invalid-enum-value error once D11 is built.

ALTER TYPE ... ADD VALUE cannot run inside a transaction block in the
Postgres versions this project has hit that error against before (see
f3a6c9d1b7e2) - transactional_ddl = False below, same as that migration.

Revision ID: d6b8e3a1f4c9
Revises: a9d4f2c7e6b3
Create Date: 2026-07-29
"""
from alembic import op

revision = "d6b8e3a1f4c9"
down_revision = "a9d4f2c7e6b3"
branch_labels = None
depends_on = None

transactional_ddl = False


def upgrade() -> None:
    op.execute("ALTER TYPE email_trigger_event_enum ADD VALUE IF NOT EXISTS 'UPLOAD_PERMANENTLY_FAILED'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type; leaving it in place is harmless.
    pass
