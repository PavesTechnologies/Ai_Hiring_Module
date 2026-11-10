"""email_notifications.provider_message_id: the SES message id of each send

Revision ID: a1c3e5f7b9d2
Revises: f9c1e3a5b7d0
Create Date: 2026-11-10

Asked for 2026-11-10 (runbook gap: stuck emails could only be matched in SES by time and recipient).
Additive: a nullable column, set when SES accepts the email (or the log-only id under EMAIL_DELIVERY=log).
Existing rows keep NULL. Downgrade drops the column.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1c3e5f7b9d2"
down_revision: Union[str, None] = "f9c1e3a5b7d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("email_notifications", sa.Column("provider_message_id", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("email_notifications", "provider_message_id")
