"""jd_skills.requirement_text: what the JD asks for under a core skill, in its own words

Revision ID: d2f4a6c8e0b1
Revises: c9e1a3b5d7f2
Create Date: 2026-10-26

JD calibration prefills each core skill's requirement text from the JD's wording
(HR edits it); the paraphrase search and the core-skill voters read it next to the
skill's description. Additive: a new nullable column, no existing value changes.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d2f4a6c8e0b1"
down_revision: Union[str, Sequence[str], None] = "c9e1a3b5d7f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jd_skills", sa.Column("requirement_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("jd_skills", "requirement_text")
