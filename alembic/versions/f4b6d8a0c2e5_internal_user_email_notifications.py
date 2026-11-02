"""email_notifications for internal users; a LOGGED status

Revision ID: f4b6d8a0c2e5
Revises: c0e2a4b6d8f1
Create Date: 2026-11-02

One email path: every email - including the embedding-health HR alert, which used to call SES
directly - is an email_notifications row sent by notifications.send_candidate_email, so each is
recorded and governed by EMAIL_DELIVERY.

- recipient_type INTERNAL_USER: a platform user (recipient_user_id, plus the email/name snapshot
  like EXTERNAL_INTERVIEWER).
- trigger_event EMBEDDING_HEALTH_ALERT.
- status LOGGED: written instead of SENT when EMAIL_DELIVERY=log - recorded, never sent.
- recipient_user_id: nullable, FK users.id.
- The recipient-shape CHECK is replaced by one that also admits INTERNAL_USER and keeps
  recipient_user_id NULL for the other two shapes (existing rows still satisfy it).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f4b6d8a0c2e5"
down_revision: Union[str, None] = "c0e2a4b6d8f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CHECK = "chk_email_notifications_recipient_shape"
_OLD_SHAPE = (
    "(recipient_type = 'CANDIDATE' AND candidate_id IS NOT NULL "
    "AND interview_interviewer_id IS NULL AND recipient_email IS NULL AND recipient_name IS NULL) "
    "OR "
    "(recipient_type = 'EXTERNAL_INTERVIEWER' AND candidate_id IS NULL "
    "AND interview_interviewer_id IS NOT NULL AND recipient_email IS NOT NULL AND recipient_name IS NOT NULL)"
)
NEW_SHAPE = (
    "(recipient_type = 'CANDIDATE' AND candidate_id IS NOT NULL AND interview_interviewer_id IS NULL "
    "AND recipient_user_id IS NULL AND recipient_email IS NULL AND recipient_name IS NULL) "
    "OR "
    "(recipient_type = 'EXTERNAL_INTERVIEWER' AND candidate_id IS NULL AND interview_interviewer_id IS NOT NULL "
    "AND recipient_user_id IS NULL AND recipient_email IS NOT NULL AND recipient_name IS NOT NULL) "
    "OR "
    "(recipient_type = 'INTERNAL_USER' AND candidate_id IS NULL AND interview_interviewer_id IS NULL "
    "AND recipient_user_id IS NOT NULL AND recipient_email IS NOT NULL AND recipient_name IS NOT NULL)"
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE email_recipient_type_enum ADD VALUE IF NOT EXISTS 'INTERNAL_USER'")
        op.execute("ALTER TYPE email_trigger_event_enum ADD VALUE IF NOT EXISTS 'EMBEDDING_HEALTH_ALERT'")
        op.execute("ALTER TYPE email_notification_status_enum ADD VALUE IF NOT EXISTS 'LOGGED'")
    op.add_column("email_notifications",
                  sa.Column("recipient_user_id", sa.String(255), sa.ForeignKey("users.id"), nullable=True))
    op.drop_constraint(_CHECK, "email_notifications", type_="check")
    op.create_check_constraint(_CHECK, "email_notifications", NEW_SHAPE)


def downgrade() -> None:
    op.drop_constraint(_CHECK, "email_notifications", type_="check")
    op.execute("DELETE FROM email_notifications WHERE recipient_type = 'INTERNAL_USER'")
    op.create_check_constraint(_CHECK, "email_notifications", _OLD_SHAPE)
    op.drop_column("email_notifications", "recipient_user_id")
    # PostgreSQL cannot drop enum values; leaving them is harmless.
