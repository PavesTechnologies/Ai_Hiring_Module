"""Required core skills (Phase 3 hard negatives)

- jd_skills.required: a core skill that may never be missing. Set in JD
  calibration (core skills only, versioned); the missing-core allowance
  covers only the core skills not marked required.

Revision ID: d1f3a5c7e9b2
Revises: c8e0a2b4d6f9
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d1f3a5c7e9b2"
down_revision: Union[str, Sequence[str], None] = "c8e0a2b4d6f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jd_skills", sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("jd_skills", "required")
