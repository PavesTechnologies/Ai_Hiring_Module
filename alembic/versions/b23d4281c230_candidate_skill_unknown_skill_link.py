"""candidate_skills unknown_skill_id link

Revision ID: b23d4281c230
Revises: c3f7a9e2d5b1
Create Date: 2026-07-24 00:00:00.000000

Adds a nullable unknown_skill_id FK on candidate_skills so an unmatched
resume skill can be traced back to the UnknownSkill row it deduped into
(the same unknown_skills pool JD unmatched skills already share), mirroring
what jd_unknown_skills does for the JD side. candidate_skills already has
one row per extracted skill (matched or not) with raw_extracted_text on it,
so a single FK column is enough here - no separate join table needed.

NOTE: this repo currently has multiple divergent alembic heads
(3e7800c51995, 44c9d277085e, b4d8e1f6a3c7, c3f7a9e2d5b1) - this migration
chains off c3f7a9e2d5b1 only (the resume-side head) and does not attempt to
merge the others.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b23d4281c230'
down_revision: Union[str, Sequence[str], None] = 'c3f7a9e2d5b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'candidate_skills',
        sa.Column('unknown_skill_id', postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        'fk_candidate_skills_unknown_skill_id',
        'candidate_skills', 'unknown_skills',
        ['unknown_skill_id'], ['id'],
    )


def downgrade() -> None:
    op.drop_constraint('fk_candidate_skills_unknown_skill_id', 'candidate_skills', type_='foreignkey')
    op.drop_column('candidate_skills', 'unknown_skill_id')
