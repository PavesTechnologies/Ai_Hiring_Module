"""resume deleted audit action

Adds RESUME_DELETED to audit_action_type_enum for the single-resume
cleanup endpoint (DELETE /resumes/{resume_id}).

Revision ID: 5439e70a5a8e
Revises: 90b05f9f2aa1
Create Date: 2026-08-04
"""
from alembic import op

revision = "5439e70a5a8e"
down_revision = "90b05f9f2aa1"
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction block.
transactional_ddl = False


def upgrade() -> None:
    op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'RESUME_DELETED'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type; leaving the new
    # value in place is harmless.
    pass
