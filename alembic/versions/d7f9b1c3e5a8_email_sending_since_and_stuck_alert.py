"""email notifications: when a send claimed the row, and a stuck-send alert trigger (batch 4 follow-up)

Revision ID: d7f9b1c3e5a8
Revises: c5e7a9b1d3f6
Create Date: 2026-11-10

Asked for 2026-11-10 (batch 4 follow-up: show emails stuck in SENDING for more than 30 minutes in the
monitoring panel and in the HR/ops alert). Additive only:

- email_notifications.sending_since (nullable timestamptz): set by the QUEUED -> SENDING claim, cleared
  when a claim is released. "Stuck" is measured from it. Rows claimed before this migration have NULL
  and fall back to created_at.
- email_trigger_event_enum gains EMAIL_SEND_STUCK_ALERT: the HR_ADMIN alert for stuck sends
  (app/tasks/email_health_tasks.py).

Same split as c5e7a9b1d3f6: the column in the normal transaction, then COMMIT before the ADD VALUE.
Downgrade drops the column; Postgres cannot drop an enum value, so EMAIL_SEND_STUCK_ALERT stays, unused.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d7f9b1c3e5a8"
down_revision: Union[str, None] = "c5e7a9b1d3f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

transactional_ddl = False


def upgrade() -> None:
    op.add_column("email_notifications", sa.Column("sending_since", sa.DateTime(timezone=True), nullable=True))

    op.execute("COMMIT")
    op.execute("ALTER TYPE email_trigger_event_enum ADD VALUE IF NOT EXISTS 'EMAIL_SEND_STUCK_ALERT'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type; leaving EMAIL_SEND_STUCK_ALERT in place is harmless.
    op.drop_column("email_notifications", "sending_since")
