"""candidate data erased audit action

Adds CANDIDATE_DATA_ERASED to audit_action_type_enum for the candidate
hard-delete/erasure endpoint (DELETE /candidates/{candidate_id}).

Revision ID: 90b05f9f2aa1
Revises: b86c7b089b02
Create Date: 2026-07-29
"""
from alembic import op

revision = "90b05f9f2aa1"
down_revision = "b86c7b089b02"
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction block.
transactional_ddl = False


def upgrade() -> None:
    op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'CANDIDATE_DATA_ERASED'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type; leaving the new
    # value in place is harmless.
    pass
