"""Add REJECTION value to composite_score_trigger_source_enum

Revision ID: 9a1c2f3e6b7d
Revises: 7043b9ed5abe
Create Date: 2026-08-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '9a1c2f3e6b7d'
down_revision: Union[str, Sequence[str], None] = '7043b9ed5abe'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Postgres 12+ allows ALTER TYPE ... ADD VALUE inside a transaction as
    # long as the new value isn't used in that same transaction - fine here,
    # since nothing in this migration inserts a row using it.
    op.execute("ALTER TYPE composite_score_trigger_source_enum ADD VALUE 'REJECTION'")


def downgrade() -> None:
    # Postgres has no ALTER TYPE ... DROP VALUE - removing an enum value
    # requires rebuilding the type (rename old -> create new -> cast every
    # dependent column -> drop old), which is only worth doing if some row
    # actually used 'REJECTION'. Left as a manual/no-op downgrade since this
    # revision is additive-only and never removes data on its own.
    pass
