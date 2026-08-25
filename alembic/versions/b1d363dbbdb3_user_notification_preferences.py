"""user notification preferences

Epic 5 Step 3 - minimal, opt-out preference table. Confirmed absent from
the DB and codebase (repo-wide grep, zero hits) before this migration.

Built ahead of any real caller, honestly - per Step 0's investigation, of
the 6 real EmailTriggerEvent values, all 5 that actually have a live send
path today (CANDIDATE_REJECTED, INTERVIEW_SCHEDULED/RESCHEDULED/
CANCELLED, CANDIDATE_SELECTED) target a candidate or external
interviewer, neither of which has a users.id row to hold a preference
against. The 6th (UPLOAD_PERMANENTLY_FAILED) is the one trigger actually
scoped for internal users (uploader + HR_ADMIN), but has zero send path
of its own yet (that's D11, unbuilt). is_notification_enabled() is built
now so it's ready the moment a real internal-user-facing send path
exists, not forced into a fake integration today - see known_issues.md's
entry naming this same "built ahead of need" shape, shared with
SHORTLIST_NOTIFICATION_BATCH_WINDOW_MINUTES.

trigger_event reuses the existing email_trigger_event_enum Postgres type
(already used by email_templates/email_notifications) rather than a
plain varchar - a typo'd trigger_event value should fail at the DB
level, not silently never match anything in is_notification_enabled()'s
lookup. Default-on (opt-out, not opt-in): an unlisted (user_id,
trigger_event) pair means enabled - this table only ever needs rows for
explicit opt-outs, so is_enabled has no meaningful default column value
of its own (every row that exists is, in practice, an opt-out).

Branched off ff1c2b57fbaf (the actual live head per `alembic current`),
not b4e7c2a91f38 - same standing multiple-unmerged-heads situation
documented in docs/known_issues.md.

Revision ID: b1d363dbbdb3
Revises: ff1c2b57fbaf
Create Date: 2026-08-18
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "b1d363dbbdb3"
down_revision = "ff1c2b57fbaf"
branch_labels = None
depends_on = None

email_trigger_event_enum = postgresql.ENUM(name="email_trigger_event_enum", create_type=False)


def upgrade() -> None:
    op.create_table(
        "user_notification_preferences",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.String(255), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("trigger_event", email_trigger_event_enum, nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "trigger_event", name="uq_user_notification_preferences_user_id_trigger_event"),
    )


def downgrade() -> None:
    op.drop_table("user_notification_preferences")
