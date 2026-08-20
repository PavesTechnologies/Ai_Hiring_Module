"""Epic 4 (M12): interview_schedules, interview_interviewers, interview_schedule_history

Adds:
  * interview_schedules — one row per campaign_candidate that has ever
    reached INTERVIEW (auto-created PENDING by StageTransitionService.
    transition()'s INTERVIEW-entry hook, filled in by the schedule
    endpoint). campaign_candidate_id is UNIQUE - a hard invariant, not a
    convention: a candidate must never have more than one live row, even
    across a fraud-review-clear re-entry into INTERVIEW.
  * interview_interviewers — free-text name/email per interviewer,
    deliberately no user_id / account resolution (confirmed decision).
  * interview_schedule_history — append-only SCHEDULED/RESCHEDULED/
    CANCELLED event log per interview. No update/delete path is given to
    it anywhere in this migration or the application code that follows -
    matching campaign_candidate_stage_history's own guarantee. Whether it
    also needs Epic 3 Fix 4's DB-level immutability trigger is a real,
    separate follow-up decision - deliberately NOT applied here.

Also adds 3 new audit_action_type_enum values: INTERVIEW_SCHEDULED,
INTERVIEW_RESCHEDULED, INTERVIEW_CANCELLED - via ADD VALUE IF NOT EXISTS,
same safety convention c1f4a7b93e20 used for its own new enum values, in
case of concurrent work adding the same labels elsewhere on this shared
instance.

Timezone: start_at/end_at/old_start_at/new_start_at are all timestamptz
(UTC) - confirmed via investigation that no timezone concept (config key,
column, or conversion utility) exists anywhere in this codebase today: all
42 files that produce timestamps use datetime.now(timezone.utc)
uniformly. Storing UTC and letting the frontend own display conversion is
the only choice consistent with that existing, unbroken precedent.

Purely additive: no existing table's columns altered, no existing row's
data touched.

Revision ID: 429b9809bc63
Revises: e9961d228f3d
Create Date: 2026-08-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '429b9809bc63'
down_revision: Union[str, Sequence[str], None] = 'e9961d228f3d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


interview_status_enum = postgresql.ENUM(
    "PENDING", "SCHEDULED", "RESCHEDULED", "COMPLETED", "CANCELLED", name="interview_status_enum",
)
interview_platform_enum = postgresql.ENUM(
    "TEAMS", "MEET", "ONSITE", "PHONE", name="interview_platform_enum",
)
interview_history_event_type_enum = postgresql.ENUM(
    "SCHEDULED", "RESCHEDULED", "CANCELLED", name="interview_history_event_type_enum",
)


def upgrade() -> None:
    # Deliberately NOT pre-created via .create(checkfirst=True) here -
    # op.create_table() below auto-creates any enum type referenced by its
    # own columns as part of the CREATE TABLE event, without checkfirst;
    # pre-creating them first caused a DuplicateObject error on the very
    # first attempt at this migration (caught and fixed before this ever
    # ran against the live DB - transactional DDL rolled the whole attempt
    # back cleanly, confirmed via a fresh check before retrying).
    op.create_table(
        "interview_schedules",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", interview_status_enum, nullable=False, server_default="PENDING"),
        sa.Column("interview_type", sa.String(length=100), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("platform", interview_platform_enum, nullable=True),
        sa.Column("meeting_link", sa.Text(), nullable=True),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column("scheduled_by", sa.String(length=255), nullable=True),
        sa.Column("scheduled_by_role", sa.String(length=50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["campaign_candidate_id"], ["campaign_candidates.id"]),
        sa.ForeignKeyConstraint(["scheduled_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_candidate_id", name="uq_interview_schedules_campaign_candidate_id"),
    )

    op.create_table(
        "interview_interviewers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("interview_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(["interview_id"], ["interview_schedules.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_interview_interviewers_interview_id", "interview_interviewers", ["interview_id"],
    )

    op.create_table(
        "interview_schedule_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("interview_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", interview_history_event_type_enum, nullable=False),
        sa.Column("old_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("new_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("changed_by", sa.String(length=255), nullable=False),
        sa.Column("changed_by_role", sa.String(length=50), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["interview_id"], ["interview_schedules.id"]),
        sa.ForeignKeyConstraint(["changed_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_interview_schedule_history_interview_id_changed_at",
        "interview_schedule_history",
        ["interview_id", "changed_at"],
    )

    for label in ("INTERVIEW_SCHEDULED", "INTERVIEW_RESCHEDULED", "INTERVIEW_CANCELLED"):
        op.execute(f"ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS '{label}'")


def downgrade() -> None:
    # audit_action_type_enum labels deliberately not removed - Postgres has
    # no DROP VALUE, and rebuilding the type would rewrite every audit_log
    # row (same reasoning c1f4a7b93e20's downgrade already documents).
    op.drop_index("ix_interview_schedule_history_interview_id_changed_at", table_name="interview_schedule_history")
    op.drop_table("interview_schedule_history")
    op.drop_index("ix_interview_interviewers_interview_id", table_name="interview_interviewers")
    op.drop_table("interview_interviewers")
    op.drop_table("interview_schedules")

    bind = op.get_bind()
    interview_history_event_type_enum.drop(bind, checkfirst=True)
    interview_platform_enum.drop(bind, checkfirst=True)
    interview_status_enum.drop(bind, checkfirst=True)
