"""interview_feedback

Revision ID: 40c2d5e7d2fe
Revises: 25a1909c01d7
Create Date: 2026-08-18 14:05:00.000000

M12 Step 3 - interview feedback, token-gated (no interviewer login).
Advisory only: nothing here touches interview_schedules.status or
campaign_candidates.pipeline_stage.

UNIQUE(interview_schedule_id, interviewer_id) is a deliberate hard lock -
one submission per interviewer per round, no silent resubmission/upsert
path, matching every other append-only guarantee already in this system.

Letting op.create_table() create the interview_feedback_recommendation_enum
type automatically (no manual pre-create) - avoids the exact
checkfirst/create_table double-create pitfall hit on 429b9809bc63.

Branched off 25a1909c01d7 (the actual live head per `alembic current`),
not b4e7c2a91f38 - same standing multiple-unmerged-heads situation
documented in docs/known_issues.md; b4e7c2a91f38 still isn't stamped live
on this shared DB.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '40c2d5e7d2fe'
down_revision: Union[str, Sequence[str], None] = '25a1909c01d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "interview_feedback",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "interview_schedule_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("interview_schedules.id"), nullable=False,
        ),
        sa.Column(
            "interviewer_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("interview_interviewers.id"), nullable=False,
        ),
        sa.Column(
            "recommendation",
            postgresql.ENUM(
                "ADVANCE", "SELECT", "REJECT", "HOLD",
                name="interview_feedback_recommendation_enum",
            ),
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "interview_schedule_id", "interviewer_id",
            name="uq_interview_feedback_interview_schedule_id_interviewer_id",
        ),
    )

    op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'INTERVIEW_FEEDBACK_SUBMITTED'")


def downgrade() -> None:
    op.drop_table("interview_feedback")
    op.execute("DROP TYPE IF EXISTS interview_feedback_recommendation_enum")
