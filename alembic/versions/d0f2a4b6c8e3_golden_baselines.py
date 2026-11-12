"""golden baselines in the database: scoring_versions.golden_baseline_run_id, scoring_version_golden_runs.results

Revision ID: d0f2a4b6c8e3
Revises: c9e1f3a5b7d2
Create Date: 2026-11-12

Settings phase 3 (approved 2026-11-12). Additive: two nullable columns. On promotion a version's passing
golden run becomes its baseline; in-app golden checks compare with the current version's baseline, the
repository's accepted run stays the CI baseline for code changes. Downgrade drops both.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d0f2a4b6c8e3"
down_revision: Union[str, None] = "c9e1f3a5b7d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("scoring_version_golden_runs",
                  sa.Column("results", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("scoring_versions", sa.Column("golden_baseline_run_id", sa.UUID(), nullable=True))
    op.create_foreign_key("fk_scoring_versions_golden_baseline_run", "scoring_versions",
                          "scoring_version_golden_runs", ["golden_baseline_run_id"], ["id"])


def downgrade() -> None:
    op.drop_constraint("fk_scoring_versions_golden_baseline_run", "scoring_versions", type_="foreignkey")
    op.drop_column("scoring_versions", "golden_baseline_run_id")
    op.drop_column("scoring_version_golden_runs", "results")
