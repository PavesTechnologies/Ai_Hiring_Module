"""jd_skills.alias_skill_ids: alias-aware JD skill matching

Stores the canonical skill ids a JD skill's aliases resolved to (e.g.
ITBM / PPM for ServiceNow SPM), so a candidate holding any of them
satisfies the skill. Nullable; existing rows keep NULL (no aliases).

Revision ID: 5a7e3c9d1b24
Revises: 8b1d4e6f2a90
Create Date: 2026-09-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "5a7e3c9d1b24"
down_revision: Union[str, Sequence[str], None] = "8b1d4e6f2a90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jd_skills",
        sa.Column("alias_skill_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("jd_skills", "alias_skill_ids")
