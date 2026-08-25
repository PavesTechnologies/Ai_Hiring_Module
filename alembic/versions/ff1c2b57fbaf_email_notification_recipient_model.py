"""email notification recipient model widening

Epic 5 Step 2 - EmailNotification widened to support two recipient
shapes (CANDIDATE / EXTERNAL_INTERVIEWER) per the Step 1 design
decision: one table, not two, since the send task's retry/dead-letter/
template-rendering machinery is ~90% recipient-agnostic and would
otherwise be duplicated for zero real benefit. A CHECK constraint
enforces exactly one recipient shape is populated, matching
recipient_type - same "don't trust service-layer discipline alone"
instinct already applied to the interview-feedback UNIQUE lock
(40c2d5e7d2fe).

interview_schedule_id (nullable FK) is a new addition beyond the
recipient-model discussion itself, added for a real correctness reason
found while wiring the send hooks: INTERVIEW_SCHEDULED/RESCHEDULED/
CANCELLED are NOT terminal, once-per-candidate events (unlike
CANDIDATE_REJECTED/CANDIDATE_SELECTED) - a candidate can have many
interview rounds, and deduping "already notified" by
campaign_candidate_id+trigger_event alone would silently skip round 2's
"interview scheduled" email because round 1's already satisfied that
check. Dedup for these 3 events is scoped to (interview_schedule_id,
trigger_event) instead - see candidate_notification_emails.py.

template_context (nullable JSONB) holds point-in-time display values
(interview_date/time/mode/interviewer_name) snapshotted at QUEUE time,
not looked up fresh at SEND time - unlike job_title/candidate_name,
which stay intentionally live-resolved. cancel() does not clear
start_at/end_at, so a send-time lookup would usually still be correct,
but a same-round reactivate-via-reschedule landing in the narrow window
before a retried send fires would silently show the NEW time on the
CANCELLED email instead of the time that was actually cancelled -
snapshotting avoids this class of race entirely, matching the same
"point-in-time fact, not current state" reasoning already behind
InterviewScheduleHistory.

Branched off 40c2d5e7d2fe (the actual live head per `alembic current`),
not b4e7c2a91f38 - same standing multiple-unmerged-heads situation
documented in docs/known_issues.md.

Revision ID: ff1c2b57fbaf
Revises: 40c2d5e7d2fe
Create Date: 2026-08-18
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "ff1c2b57fbaf"
down_revision = "40c2d5e7d2fe"
branch_labels = None
depends_on = None

email_recipient_type_enum = postgresql.ENUM(
    "CANDIDATE", "EXTERNAL_INTERVIEWER", name="email_recipient_type_enum",
)


def upgrade() -> None:
    email_recipient_type_enum.create(op.get_bind(), checkfirst=True)

    op.alter_column("email_notifications", "candidate_id", nullable=True)

    op.add_column(
        "email_notifications",
        sa.Column("recipient_type", email_recipient_type_enum, nullable=False, server_default="CANDIDATE"),
    )
    op.add_column(
        "email_notifications",
        sa.Column(
            "interview_interviewer_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("interview_interviewers.id"), nullable=True,
        ),
    )
    op.add_column("email_notifications", sa.Column("recipient_email", sa.String(255), nullable=True))
    op.add_column("email_notifications", sa.Column("recipient_name", sa.String(255), nullable=True))
    op.add_column("email_notifications", sa.Column("template_context", postgresql.JSONB(), nullable=True))
    op.add_column(
        "email_notifications",
        sa.Column(
            "interview_schedule_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("interview_schedules.id"), nullable=True,
        ),
    )

    op.create_check_constraint(
        "chk_email_notifications_recipient_shape",
        "email_notifications",
        "(recipient_type = 'CANDIDATE' AND candidate_id IS NOT NULL "
        "AND interview_interviewer_id IS NULL AND recipient_email IS NULL AND recipient_name IS NULL) "
        "OR "
        "(recipient_type = 'EXTERNAL_INTERVIEWER' AND candidate_id IS NULL "
        "AND interview_interviewer_id IS NOT NULL AND recipient_email IS NOT NULL AND recipient_name IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("chk_email_notifications_recipient_shape", "email_notifications", type_="check")
    op.drop_column("email_notifications", "interview_schedule_id")
    op.drop_column("email_notifications", "template_context")
    op.drop_column("email_notifications", "recipient_name")
    op.drop_column("email_notifications", "recipient_email")
    op.drop_column("email_notifications", "interview_interviewer_id")
    op.drop_column("email_notifications", "recipient_type")
    op.alter_column("email_notifications", "candidate_id", nullable=False)
    email_recipient_type_enum.drop(op.get_bind(), checkfirst=True)
