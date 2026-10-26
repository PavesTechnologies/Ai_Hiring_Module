"""audit_log.campaign_id: no foreign key - a bare id like entity_id

Revision ID: b7d9f1a3c5e8
Revises: d6f8a0c2e4b7
Create Date: 2026-10-23

audit_log is append-only (triggers refuse UPDATE/DELETE/TRUNCATE), so a
foreign key from it made any audited campaign undeletable: removing the
campaign would need audit rows changed. campaign_id becomes a bare id, the
same way entity_id already is - the audit trail keeps the id of a campaign
that no longer exists. The index on (campaign_id, created_at) stays.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "b7d9f1a3c5e8"
down_revision: Union[str, Sequence[str], None] = "d6f8a0c2e4b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("audit_log_campaign_id_fkey", "audit_log", type_="foreignkey")


def downgrade() -> None:
    # Fails if an audit row names a campaign that has since been deleted - by design.
    op.create_foreign_key("audit_log_campaign_id_fkey", "audit_log", "hiring_campaigns",
                          ["campaign_id"], ["id"])
