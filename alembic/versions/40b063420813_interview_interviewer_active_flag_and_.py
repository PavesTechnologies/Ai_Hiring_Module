"""interview_interviewer_active_flag_and_lifecycle_triggers

Reopens the "lingering interviewer" edge case from the reschedule-crash
fix: replace_interviewers() previously either hard-deleted a removed
interviewer's row or, if it was already referenced by an interview_feedback/
email_notifications row, silently skipped the delete - leaving that row
linked to the round forever with no way to tell "removed" apart from
"still on the round." Adds is_active so removal can always be recorded
(soft-delete, regardless of reference state) instead of two different
code paths depending on what happened to have referenced the row already.

Also adds the 3 new email_trigger_event_enum values this reopens: an
interviewer invitation email (new - sent per-interviewer when they're
added to a round, at schedule() or reschedule() time), a removal notice
(sent when replace_interviewers() sets is_active=false), and a
cancellation notice distinct from the candidate-facing INTERVIEW_CANCELLED
(sent to every still-active interviewer on a cancelled round).

Same split as e686c750b7b4: the ADD COLUMN runs in the normal transaction,
then an explicit COMMIT closes it before the ADD VALUE statements, since
Postgres won't let the same transaction that adds an enum value also use
it - matching the ~25 other enum-value migrations in this codebase (see
e686c750b7b4's own docstring for why the transactional_ddl module
attribute below is cosmetic only, not functional).

Revision ID: 40b063420813
Revises: c03d249037ca
Create Date: 2026-08-21
"""
from alembic import op
import sqlalchemy as sa


revision = "40b063420813"
down_revision = "c03d249037ca"
branch_labels = None
depends_on = None

transactional_ddl = False


def upgrade() -> None:
    op.add_column(
        "interview_interviewers",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )

    op.execute("COMMIT")
    op.execute("ALTER TYPE email_trigger_event_enum ADD VALUE IF NOT EXISTS 'INTERVIEW_INTERVIEWER_INVITATION'")
    op.execute("ALTER TYPE email_trigger_event_enum ADD VALUE IF NOT EXISTS 'INTERVIEW_INTERVIEWER_REMOVED'")
    op.execute("ALTER TYPE email_trigger_event_enum ADD VALUE IF NOT EXISTS 'INTERVIEW_INTERVIEWER_CANCELLED'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type; leaving it in place is harmless.
    op.drop_column("interview_interviewers", "is_active")
