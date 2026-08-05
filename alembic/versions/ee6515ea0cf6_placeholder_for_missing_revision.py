"""placeholder for missing revision

alembic_version was found stamped at 'ee6515ea0cf6' - a revision that does
not exist as a migration file anywhere in this repo (same class of issue
as a1c6d9f3e7b2's and a5f3d8c1b6e4's own "placeholder for missing
revision" fixes: someone applied a schema change directly against the
shared dev DB and stamped alembic_version by hand, without ever
committing the migration file).

Verified against the live schema: audit_action_type_enum already contains
'CONSENT_RECORDED' (not present in any migration file), immediately
alongside c95f3a5b4b35's own 'CONSENT' addition to audit_entity_type_enum
- the two clearly belong to the same consent-tracking change, applied
together. No other unexplained schema drift was found beyond this single
enum value, so this is authored as a no-op placeholder (the change
already happened live) rather than a guess at unrelated schema.

Revision ID: ee6515ea0cf6
Revises: c95f3a5b4b35
Create Date: 2026-08-04
"""
from alembic import op

revision = "ee6515ea0cf6"
down_revision = "c95f3a5b4b35"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'CONSENT_RECORDED'")


def downgrade() -> None:
    # PostgreSQL doesn't support removing enum values safely.
    pass
