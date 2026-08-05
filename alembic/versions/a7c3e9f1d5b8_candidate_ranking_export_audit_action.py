"""candidate ranking export audit action (M10-E03 Phase 3)

Adds CANDIDATE_RANKING_EXPORTED to audit_action_type_enum for the new
GET /campaign-candidates/campaign/{campaign_id}/export endpoint - the only
schema change Phase 3 requires. No ranking/composite schema change, no new
table, no repository change.

Revision ID: a7c3e9f1d5b8
Revises: f1a3c7e9b2d4
Create Date: 2026-08-05
"""
from alembic import op

revision = "a7c3e9f1d5b8"
down_revision = "f1a3c7e9b2d4"
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction block.
transactional_ddl = False


def upgrade() -> None:
    op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'CANDIDATE_RANKING_EXPORTED'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type; leaving the new
    # value in place is harmless.
    pass
