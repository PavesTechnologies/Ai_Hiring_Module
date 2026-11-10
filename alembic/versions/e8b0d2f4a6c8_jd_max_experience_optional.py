"""job_descriptions.max_experience_years nullable (review item 27)

Revision ID: e8b0d2f4a6c8
Revises: d7f9b1c3e5a8
Create Date: 2026-11-10

Approved 2026-11-05 (review item 27: "max_experience_years nullable"). A maximum experience correlates
with age, so it must never reject or mark down a candidate: the JD form's maximum becomes optional, and
no scoring layer, embedding text or AI evaluator input uses it. Existing values are kept (no rewrite).

Downgrade sets NOT NULL again; it fails while any JD has no maximum (fill those first).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e8b0d2f4a6c8"
down_revision: Union[str, None] = "d7f9b1c3e5a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("job_descriptions", "max_experience_years", existing_type=sa.Numeric(4, 1), nullable=True)


def downgrade() -> None:
    op.alter_column("job_descriptions", "max_experience_years", existing_type=sa.Numeric(4, 1), nullable=False)
