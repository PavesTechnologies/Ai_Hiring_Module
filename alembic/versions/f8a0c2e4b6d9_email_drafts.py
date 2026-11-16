"""email drafts: candidate and interviewer emails wait for a person's Send

Revision ID: f8a0c2e4b6d9
Revises: e2a4c6d8f0b1
Create Date: 2026-11-13

Email plan, phase 3 (approved 2026-11-13, including the index rebuild):
  - email_notification_status_enum: DRAFT (prepared, waiting for Send), DISCARDED (cancelled by a person or
    replaced by a newer draft), BLOCKED (refused by the checks at send time);
  - email_notifications: prepared_by / prepared_at, confirmed_by / confirmed_at (who clicked Send, when),
    preview_hash (a hash of the rendered subject, body and recipient the person previewed - the email itself
    is not stored), block_code (the checks' codes when BLOCKED); an index for listing a candidate's emails by
    status;
  - uq_email_notifications_dedup_key rebuilt (drop + create, approved): DISCARDED and BLOCKED rows no longer
    count as live, like FAILED, so a discarded or blocked email doesn't stop a new one;
  - audit_action_type_enum: EMAIL_DRAFT_SENT, EMAIL_DRAFT_DISCARDED.

Downgrade refuses while any row is DRAFT, DISCARDED or BLOCKED (the old index and code can't represent them);
it never rewrites or deletes an email. Enum values stay (Postgres can't drop them).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f8a0c2e4b6d9"
down_revision: Union[str, None] = "e2a4c6d8f0b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for value in ("DRAFT", "DISCARDED", "BLOCKED"):
            op.execute(f"ALTER TYPE email_notification_status_enum ADD VALUE IF NOT EXISTS '{value}'")
        for value in ("EMAIL_DRAFT_SENT", "EMAIL_DRAFT_DISCARDED"):
            op.execute(f"ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS '{value}'")

    op.add_column("email_notifications", sa.Column("prepared_by", sa.String(255), sa.ForeignKey("users.id"), nullable=True))
    op.add_column("email_notifications", sa.Column("prepared_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("email_notifications", sa.Column("confirmed_by", sa.String(255), sa.ForeignKey("users.id"), nullable=True))
    op.add_column("email_notifications", sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("email_notifications", sa.Column("preview_hash", sa.String(64), nullable=True))
    op.add_column("email_notifications", sa.Column("block_code", sa.String(255), nullable=True))
    op.create_index("ix_email_notifications_campaign_candidate_status", "email_notifications",
                    ["campaign_candidate_id", "status"])

    op.drop_index("uq_email_notifications_dedup_key", table_name="email_notifications")
    op.create_index(
        "uq_email_notifications_dedup_key", "email_notifications", ["dedup_key"], unique=True,
        postgresql_where=sa.text("dedup_key IS NOT NULL AND status NOT IN ('FAILED', 'DISCARDED', 'BLOCKED')"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    count = bind.execute(sa.text(
        "SELECT count(*) FROM email_notifications WHERE status::text IN ('DRAFT', 'DISCARDED', 'BLOCKED')")).scalar()
    if count:
        raise RuntimeError(f"Refusing to downgrade: {count} email(s) are DRAFT, DISCARDED or BLOCKED, which the "
                           "previous schema can't represent. Nothing was changed.")
    op.drop_index("uq_email_notifications_dedup_key", table_name="email_notifications")
    op.create_index(
        "uq_email_notifications_dedup_key", "email_notifications", ["dedup_key"], unique=True,
        postgresql_where=sa.text("dedup_key IS NOT NULL AND status <> 'FAILED'"),
    )
    op.drop_index("ix_email_notifications_campaign_candidate_status", table_name="email_notifications")
    for column in ("block_code", "preview_hash", "confirmed_at", "confirmed_by", "prepared_at", "prepared_by"):
        op.drop_column("email_notifications", column)
