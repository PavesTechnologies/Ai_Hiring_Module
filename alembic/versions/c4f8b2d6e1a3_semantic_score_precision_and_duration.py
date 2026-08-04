"""semantic_score precision, semantic_score_computed_at, celery_task_log duration_ms

Tasks 538/539: semantic scoring is now computed entirely in Postgres via
pgvector (1 - (resume_embedding <=> jd_embedding)) and stored at NUMERIC(5,4)
precision (was NUMERIC(7,6)) - four decimal places is what the score
actually needs, and narrower precision is what Task 538 explicitly calls
for. semantic_score_computed_at tracks when that stored score was last
computed, set atomically alongside semantic_score/updated_at. duration_ms
on celery_task_log records how long the similarity computation itself took.

No existing campaign_candidates rows have a non-null semantic_score yet
(semantic scoring is new this module), so narrowing the NUMERIC scale has
no data to round/truncate.

Revision ID: c4f8b2d6e1a3
Revises: b3e7a1c9d5f2
Create Date: 2026-08-04
"""
from alembic import op
import sqlalchemy as sa

revision = "c4f8b2d6e1a3"
down_revision = "b3e7a1c9d5f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "campaign_candidates", "semantic_score",
        type_=sa.Numeric(5, 4),
        existing_type=sa.Numeric(7, 6),
        existing_nullable=True,
    )
    op.add_column(
        "campaign_candidates",
        sa.Column("semantic_score_computed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "celery_task_log",
        sa.Column("duration_ms", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("celery_task_log", "duration_ms")
    op.drop_column("campaign_candidates", "semantic_score_computed_at")
    op.alter_column(
        "campaign_candidates", "semantic_score",
        type_=sa.Numeric(7, 6),
        existing_type=sa.Numeric(5, 4),
        existing_nullable=True,
    )
