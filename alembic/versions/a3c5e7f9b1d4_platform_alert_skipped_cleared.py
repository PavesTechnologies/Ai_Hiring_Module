"""audit actions PLATFORM_ALERT_SKIPPED and PLATFORM_ALERT_CLEARED

Revision ID: a3c5e7f9b1d4
Revises: f4b6d8a0c2e5
Create Date: 2026-11-02

The embedding-health alert has a cooldown: it emails once when a campaign crosses the threshold,
again only if the campaign is still over it 24 hours after the last email, or after it recovers
and crosses again. Each check that holds an alert back is audited (PLATFORM_ALERT_SKIPPED), and a
campaign back under the threshold after an alert is audited too (PLATFORM_ALERT_CLEARED) - that is
what makes the next crossing a new one. Additive: two enum values.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "a3c5e7f9b1d4"
down_revision: Union[str, None] = "f4b6d8a0c2e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'PLATFORM_ALERT_SKIPPED'")
        op.execute("ALTER TYPE audit_action_type_enum ADD VALUE IF NOT EXISTS 'PLATFORM_ALERT_CLEARED'")


def downgrade() -> None:
    # PostgreSQL cannot drop enum values; leaving them is harmless.
    pass
