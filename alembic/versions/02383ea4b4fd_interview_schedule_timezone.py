"""interview_schedule_timezone

Real timezone-discrepancy fix. Previously ScheduleInterviewRequest/
RescheduleInterviewRequest carried a bare date/start_time/end_time with no
timezone field at all, and _combine_utc() just tagged whatever numbers the
client sent with tzinfo=UTC - a relabel, not a conversion. Every downstream
reader (calendar invite payload, notification emails) then echoed those
mislabeled numbers, so they all agreed with each other but none of them
were actually correct - and functional gates (request_feedback/complete's
"has this interview actually happened yet" checks, the feedback-request
sweep) compared this fake-UTC value against a real
datetime.now(timezone.utc), firing early/late by the true UTC offset.

Adds `timezone` (IANA zone name, e.g. "Asia/Kolkata") to interview_schedules
so a real date+time->UTC conversion can happen at write time and be
reversed correctly at read time (API response) and at render time
(notification emails). NOT NULL DEFAULT 'UTC' for schema simplicity - this
default is a technical placeholder for pre-existing rows only, not a claim
that their start_at/end_at were actually scheduled in UTC. There is no way
to recover what a historical row's real intended timezone was; going
forward every schedule()/reschedule() call sets this for real from the
now-required request field.

Revision ID: 02383ea4b4fd
Revises: 40b063420813
Create Date: 2026-08-24
"""
from alembic import op
import sqlalchemy as sa


revision = "02383ea4b4fd"
down_revision = "40b063420813"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "interview_schedules",
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="UTC"),
    )


def downgrade() -> None:
    op.drop_column("interview_schedules", "timezone")
