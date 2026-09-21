"""interview_interviewer_rescheduled_enum

Reschedule-notification gap fix: an interviewer who was already invited to
a round and stays on it through a reschedule previously received nothing.
INVITATION is deduped per (interview_schedule_id, interviewer_id) - it
only fires once per interviewer per round, so re-sending it on reschedule
is silently skipped as "already invited." Only the candidate (via
INTERVIEW_RESCHEDULED) and any brand-new interviewer added during the
reschedule (via their first INVITATION) were ever actually notified.

Adds INTERVIEW_INTERVIEWER_RESCHEDULED so InterviewScheduleService.
reschedule() can notify an already-invited interviewer distinctly from a
newly-added one, mirroring the CANCELLED event's existing candidate/
interviewer split.

Revision ID: c4d8e21a9f6b
Revises: 7f2c4a8e91b3
Create Date: 2026-09-18
"""
from alembic import op

revision = "c4d8e21a9f6b"
down_revision = "7f2c4a8e91b3"
branch_labels = None
depends_on = None

transactional_ddl = False


def upgrade() -> None:
    op.execute("COMMIT")
    op.execute("ALTER TYPE email_trigger_event_enum ADD VALUE IF NOT EXISTS 'INTERVIEW_INTERVIEWER_RESCHEDULED'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type; leaving it in place is harmless.
    pass
