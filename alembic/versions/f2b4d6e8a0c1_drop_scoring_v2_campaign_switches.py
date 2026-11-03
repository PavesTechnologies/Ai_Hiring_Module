"""drop hiring_campaigns.scoring_v2_enabled and scoring_v2_shadow (retire v1, phase E - migration 3)

Revision ID: f2b4d6e8a0c1
Revises: e1a3c5e7f9b2
Create Date: 2026-11-03

The per-campaign v2 switches stopped meaning anything in phases C and D: v2 (the current scoring
version) decides every campaign, and shadow mode is hiring_campaigns.candidate_scoring_version_id.
Nothing in the code reads or writes them since phase E part 1.

Dropped (no index, constraint or view depends on either):
- hiring_campaigns.scoring_v2_enabled  boolean NOT NULL DEFAULT false
- hiring_campaigns.scoring_v2_shadow   boolean NOT NULL DEFAULT false

Dev, 2026-11-03: 3 campaigns, all with (true, true) - [P3] Junior Python Developer, [P3] ServiceNow ITSM,
[SENIOR] Head of Data & AI Platforms. A fresh database has the columns at their defaults only.

Downgrade re-adds both columns with their old type and default (false); the values aren't kept - no
code reads them.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f2b4d6e8a0c1"
down_revision: Union[str, None] = "e1a3c5e7f9b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("hiring_campaigns", "scoring_v2_shadow")
    op.drop_column("hiring_campaigns", "scoring_v2_enabled")


def downgrade() -> None:
    op.add_column("hiring_campaigns", sa.Column("scoring_v2_enabled", sa.Boolean(), nullable=False,
                                                server_default=sa.text("false")))
    op.add_column("hiring_campaigns", sa.Column("scoring_v2_shadow", sa.Boolean(), nullable=False,
                                                server_default=sa.text("false")))
