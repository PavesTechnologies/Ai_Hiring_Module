"""Scoring v2: skill_pattern_rules (wording patterns -> DERIVED skill evidence)

Additive only. Rows are seeded by app/seeds/seed_scoring_v2.py.

Revision ID: c4e8a2f6d9b1
Revises: a9d3f7c2e5b8
Create Date: 2026-10-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c4e8a2f6d9b1"
down_revision: Union[str, Sequence[str], None] = "a9d3f7c2e5b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "skill_pattern_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("canonical_skill_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("skill_ontology.id"), nullable=False),
        sa.Column("name", sa.String(150), nullable=False, unique=True),
        sa.Column("anchor_terms", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("near_terms", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("max_gap_words", sa.Integer(), nullable=False, server_default=sa.text("3")),
        sa.Column("sections", postgresql.ARRAY(sa.String(30)), nullable=False),
        sa.Column("anchor_case_sensitive", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("skill_pattern_rules")
