"""add consent to audit_entity_type_enum

Revision ID: dee0ef573689
Revises: c4f8b2d6e1a3
Create Date: 2026-08-04 12:50:06.347749

NOTE (2026-08-06): originally authored with revision id "c95f3a5b4b35",
which collided with an unrelated placeholder migration
(c95f3a5b4b35_placeholder_for_stamped_but_unknown_revision.py) - alembic
reported "Revision c95f3a5b4b35 is present more than once" and could not
resolve a single head. Renamed to dee0ef573689; down_revision/content
otherwise unchanged. Confirmed via direct DB inspection that
audit_entity_type_enum already contains 'CONSENT', so this is folded into
e686c750b7b4's merge as a no-op rather than re-run.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "dee0ef573689"
down_revision: Union[str, Sequence[str], None] = "c4f8b2d6e1a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TYPE audit_entity_type_enum
        ADD VALUE IF NOT EXISTS 'CONSENT';
    """)


def downgrade() -> None:
    # PostgreSQL doesn't support removing enum values safely.
    pass