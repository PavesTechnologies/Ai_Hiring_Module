"""Skill "not this skill" text for the confirmation voters

- skill_ontology.not_description: what the skill is not (e.g. Incident
  Management: "not handling customer support cases"). Shown only to the
  3-vote confirmation, never used in the paraphrase search, where it would
  dilute the similarity of real matches. Nullable; edited by HR.

Revision ID: f3b5d7f9a1c4
Revises: e2a4c6e8f0b3
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f3b5d7f9a1c4"
down_revision: Union[str, Sequence[str], None] = "e2a4c6e8f0b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("skill_ontology", sa.Column("not_description", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("skill_ontology", "not_description")
