"""interview_per_recipient_timezone

Per-recipient timezone fix, follow-up to 02383ea4b4fd (interview_schedule_
timezone). That migration made start_at/end_at a genuine UTC instant and
added `timezone` for reversing it back to wall-clock - but every
notification email (candidate and interviewer alike) reverses using that
SAME single `timezone` value, i.e. the zone the SCHEDULER declared. A
candidate or interviewer in a different zone than the scheduler still
gets shown the wrong local time in their email, even though the
underlying instant is correct.

Adds two nullable columns:
- interview_schedules.candidate_timezone - the candidate's own IANA zone
  for this round, if known. NULL means "same as `timezone`" (today's
  behavior), so nothing changes for existing rows or callers that don't
  send one.
- interview_interviewers.timezone - this interviewer's own IANA zone.
  Same NULL-means-fallback semantics. There's deliberately no account
  resolution for interviewers (see InterviewInterviewer's own docstring -
  they're free-text name/email, not linked to a user), so this is the
  only place their timezone can ever be captured.

Both nullable with no default - unlike interview_schedules.timezone
(NOT NULL DEFAULT 'UTC'), there's no reasonable default for "this specific
person's timezone", and NULL already has a correct, intentional meaning
here (fall back to the round's own timezone) rather than being a gap to
paper over.

Revision ID: 7f2c4a8e91b3
Revises: 02383ea4b4fd
Create Date: 2026-09-18
"""
from alembic import op
import sqlalchemy as sa


revision = "7f2c4a8e91b3"
down_revision = "02383ea4b4fd"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "interview_schedules",
        sa.Column("candidate_timezone", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "interview_interviewers",
        sa.Column("timezone", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("interview_interviewers", "timezone")
    op.drop_column("interview_schedules", "candidate_timezone")
