"""add campaign_weight_presets table

Revision ID: 44c9d277085e
Revises: 3a2069bb5471
Create Date: 2026-07-21 00:00:00.000000

Root cause: app/models/campaign_weight_preset.py (CampaignWeightPreset)
was never imported in alembic/env.py's explicit model-import list, so it
was never registered onto Base.metadata for Alembic's autogenerate to see
- no migration was ever generated or applied for it, even though the
model and the repository/service code that queries it have existed all
along. This migration creates exactly what the model already declares;
no business logic changed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '44c9d277085e'
down_revision: Union[str, Sequence[str], None] = '3a2069bb5471'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'campaign_weight_presets',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('org_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('description', sa.String(length=255), nullable=True),
        sa.Column('weight_deterministic', sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column('weight_semantic', sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column('weight_ai', sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column('deterministic_threshold', sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column('semantic_threshold', sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column('ai_threshold', sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column('created_by', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('campaign_weight_presets_created_by_fkey')),
        sa.PrimaryKeyConstraint('id', name=op.f('campaign_weight_presets_pkey')),
        sa.UniqueConstraint('org_id', 'name', name='uq_campaign_weight_presets_org_name'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('campaign_weight_presets')
