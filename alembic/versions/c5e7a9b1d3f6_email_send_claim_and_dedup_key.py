"""email notifications: a SENDING claim state, and a dedup key with a partial unique index (review item 7)

Revision ID: c5e7a9b1d3f6
Revises: b4d6f8a0c2e5
Create Date: 2026-11-09

Approved 2026-11-05 (review item 7: "a partial unique index on the dedup columns"). Additive only:

- email_notification_status_enum gains SENDING. The send task claims a QUEUED row (an atomic
  QUEUED -> SENDING update) before it calls SES, so a redelivered or retried task never sends the same
  row twice; a row left SENDING is never sent again automatically.
- email_notifications.dedup_key (nullable): what makes two notifications "the same email" - set by every
  automatic queuer (app/services/notifications/dedup.py); NULL for manual re-sends, for internal-user
  alerts, and for rows queued before this migration (no backfill).
- uq_email_notifications_dedup_key: unique on dedup_key while the row is live (status <> 'FAILED'), so two
  schedulers or two overlapping sweeps can never both queue the same email. A FAILED row frees its key
  for a new attempt, as before.

Same split as 40b063420813: the column and index in the normal transaction, then COMMIT before the
ADD VALUE (Postgres won't use a new enum value in the transaction that added it; nothing here uses it).

Downgrade drops the index and the column. Postgres cannot drop an enum value; SENDING stays, unused.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c5e7a9b1d3f6"
down_revision: Union[str, None] = "b4d6f8a0c2e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

transactional_ddl = False


def upgrade() -> None:
    op.add_column("email_notifications", sa.Column("dedup_key", sa.String(255), nullable=True))
    op.create_index(
        "uq_email_notifications_dedup_key", "email_notifications", ["dedup_key"], unique=True,
        postgresql_where=sa.text("dedup_key IS NOT NULL AND status <> 'FAILED'"),
    )

    op.execute("COMMIT")
    op.execute("ALTER TYPE email_notification_status_enum ADD VALUE IF NOT EXISTS 'SENDING'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type; leaving SENDING in place is harmless.
    op.drop_index("uq_email_notifications_dedup_key", table_name="email_notifications")
    op.drop_column("email_notifications", "dedup_key")
