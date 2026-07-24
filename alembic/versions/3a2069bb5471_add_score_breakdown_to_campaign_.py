"""add score_breakdown to campaign candidates

Revision ID: 3a2069bb5471
Revises: 83b9f848edcf
Create Date: 2026-07-17 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '3a2069bb5471'
down_revision: Union[str, Sequence[str], None] = '83b9f848edcf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'campaign_candidates',
        sa.Column('score_breakdown', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('campaign_candidates', 'score_breakdown')
