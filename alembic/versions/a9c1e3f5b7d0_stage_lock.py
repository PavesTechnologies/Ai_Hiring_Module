"""stage lock: once a rejection or selection email is sent, the candidate's stage in that campaign is locked

Revision ID: a9c1e3f5b7d0
Revises: f8a0c2e4b6d9
Create Date: 2026-11-16

Email plan, phase 4 (approved 2026-11-13), all additive:
  - campaign_candidates: stage_locked_at, stage_lock_reason (REJECTION_EMAIL_SENT / SELECTION_EMAIL_SENT),
    stage_lock_notification_id (the email that locked it);
  - a trigger refusing any pipeline_stage change on a locked row (the database backs up the code: a path missed
    in the code still can't move a locked candidate). An unlock clears stage_locked_at first;
  - override_reason_codes.kind (default OVERRIDE, so every existing code keeps its meaning) and three STAGE_UNLOCK
    codes for the HR admin unlock;
  - audit_action_type_enum: STAGE_UNLOCKED.

No existing row is locked by this migration (no backfill). Downgrade refuses while any candidate is locked or any
STAGE_UNLOCK code is in use by an audit entry; it never rewrites data. Enum values stay.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a9c1e3f5b7d0"
down_revision: Union[str, None] = "f8a0c2e4b6d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UNLOCK_CODES = (
    ("EMAIL_SENT_IN_ERROR", "The email was sent in error", 1),
    ("CANDIDATE_REENGAGED", "The candidate is back in consideration", 2),
    ("DATA_CORRECTION", "Correcting the candidate's record", 3),
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'STAGE_UNLOCKED'")

    op.add_column("campaign_candidates", sa.Column("stage_locked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("campaign_candidates", sa.Column("stage_lock_reason", sa.String(40), nullable=True))
    op.add_column("campaign_candidates", sa.Column(
        "stage_lock_notification_id", sa.dialects.postgresql.UUID(as_uuid=True),
        sa.ForeignKey("email_notifications.id"), nullable=True))
    op.execute("""
        CREATE FUNCTION refuse_locked_stage_change() RETURNS trigger AS $$
        BEGIN
            IF OLD.stage_locked_at IS NOT NULL AND NEW.stage_locked_at IS NOT NULL
               AND NEW.pipeline_stage IS DISTINCT FROM OLD.pipeline_stage THEN
                RAISE EXCEPTION 'STAGE_LOCKED: campaign candidate % is locked (%)', OLD.id, OLD.stage_lock_reason
                    USING ERRCODE = 'P0001';
            END IF;
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_campaign_candidates_stage_lock
        BEFORE UPDATE OF pipeline_stage ON campaign_candidates
        FOR EACH ROW EXECUTE FUNCTION refuse_locked_stage_change()
    """)

    op.add_column("override_reason_codes", sa.Column("kind", sa.String(20), nullable=False, server_default="OVERRIDE"))
    for code, label, order in UNLOCK_CODES:
        op.execute(sa.text(
            "INSERT INTO override_reason_codes (code, label, is_active, sort_order, kind) "
            "VALUES (:code, :label, true, :order, 'STAGE_UNLOCK') ON CONFLICT (code) DO NOTHING"
        ).bindparams(code=code, label=label, order=order))


def downgrade() -> None:
    bind = op.get_bind()
    locked = bind.execute(sa.text("SELECT count(*) FROM campaign_candidates WHERE stage_locked_at IS NOT NULL")).scalar()
    if locked:
        raise RuntimeError(f"Refusing to downgrade: {locked} candidate(s) are stage-locked. Nothing was changed.")
    used = bind.execute(sa.text(
        "SELECT count(*) FROM audit_logs WHERE action_type::text = 'STAGE_UNLOCKED'")).scalar()
    if used:
        raise RuntimeError(f"Refusing to downgrade: {used} stage unlock(s) are audited with STAGE_UNLOCK codes. "
                           "Nothing was changed.")
    op.execute("DELETE FROM override_reason_codes WHERE kind = 'STAGE_UNLOCK'")
    op.drop_column("override_reason_codes", "kind")
    op.execute("DROP TRIGGER trg_campaign_candidates_stage_lock ON campaign_candidates")
    op.execute("DROP FUNCTION refuse_locked_stage_change()")
    op.drop_column("campaign_candidates", "stage_lock_notification_id")
    op.drop_column("campaign_candidates", "stage_lock_reason")
    op.drop_column("campaign_candidates", "stage_locked_at")
