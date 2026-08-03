"""add report_scheduled to hiring_campaigns

Merges the 8 current branch heads and adds hiring_campaigns.report_scheduled,
which the HiringCampaign model (app/models/campaigns.py) declares but no
prior migration ever created - campaign creation was failing with
psycopg2.errors.UndefinedColumn on this column.

Revision ID: 8045be7751f1
Revises: 3ae81a38abdc, 3e7800c51995, 90b05f9f2aa1, b4d8e1f6a3c7, d4b7f1c8a3e6, d6b8e3a1f4c9, e2c8a4f6b9d1, f5b8d2c4a917
Create Date: 2026-07-31 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8045be7751f1'
down_revision: Union[str, Sequence[str], None] = (
    '3ae81a38abdc', '3e7800c51995', '90b05f9f2aa1', 'b4d8e1f6a3c7',
    'd4b7f1c8a3e6', 'd6b8e3a1f4c9', 'e2c8a4f6b9d1', 'f5b8d2c4a917',
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("hiring_campaigns")}
    if "report_scheduled" not in columns:
        op.add_column(
            'hiring_campaigns',
            sa.Column('report_scheduled', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('hiring_campaigns', 'report_scheduled')
