"""campaign_candidates.deterministic_breakdown in v2's own shape (retire v1, phase C)

Revision ID: d8f0a2c4e6b9
Revises: c6e8a0b2d4f7
Create Date: 2026-11-02

v1 stops running in phase C, and every reader moves to v2's breakdown. Until now a v2 decision was
stored v1-shaped: v1's breakdown with v2's numbers copied over and v2's own breakdown nested under
"v2" (acted_on_version = "v2"). This makes the stored breakdown v2's own:

- campaign_candidates.deterministic_breakdown := deterministic_breakdown -> 'v2', for every row where
  acted_on_version = 'v2' and a 'v2' breakdown is present. The 20 v1-only keys go (score_passed,
  core_gap_passed, coverage_passed, technical_score, gate_skill_scope, NO_VERIFIED_SKILLS,
  max_missing_core_pct, mandatory_skill_score, preferred_skill_bonus, mandatory_coverage_pct,
  v1_deterministic_score, v1_deterministic_passed, max_missing_core_skills, semantic_tier_available,
  skill_deterministic_score, skill_qualification_passed, domain_capability_validation,
  required_skill_coverage_threshold, acted_on_version, v2); mandatory_skills, preferred_skills,
  core_coverage_pct, missing_core_skill_count, allowed_missing_core_skills and required_core_missing
  take v2's values (the top-level copies were v1's).
- deterministic_score / deterministic_passed are not touched (they already hold v2's values).
- Stage-history snapshots, audit entries and candidate_scoring_results are not touched (history).

Every replaced breakdown is kept first in campaign_candidate_breakdown_backup (new table), which the
downgrade restores.

Irreversible since b4d6f8a0c2e5 dropped the backup table (approved 2026-11-03): downgrade stops.

A fresh (production) database has no such rows: the update affects 0 rows there.
Dev, 2026-11-02: 34 rows (all 34 campaign candidates; each one's nested v2 breakdown equals its latest
acted-on v2 result row - score and outcome).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d8f0a2c4e6b9"
down_revision: Union[str, None] = "c6e8a0b2d4f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_V2_ACTED = "deterministic_breakdown ? 'v2' AND deterministic_breakdown->>'acted_on_version' = 'v2'"


def upgrade() -> None:
    op.create_table(
        "campaign_candidate_breakdown_backup",
        sa.Column("campaign_candidate_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("deterministic_breakdown", postgresql.JSONB(), nullable=False),
        sa.Column("reason", sa.String(60), nullable=False),
        sa.Column("saved_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.execute(f"""
        INSERT INTO campaign_candidate_breakdown_backup (campaign_candidate_id, deterministic_breakdown, reason)
        SELECT id, deterministic_breakdown, 'retire_v1_phase_c_v2_shape'
        FROM campaign_candidates WHERE {_V2_ACTED}
    """)
    op.execute(f"""
        UPDATE campaign_candidates SET deterministic_breakdown = deterministic_breakdown -> 'v2'
        WHERE {_V2_ACTED}
    """)


def downgrade() -> None:
    # It restored the v1-shaped breakdowns from campaign_candidate_breakdown_backup, which migration
    # b4d6f8a0c2e5 dropped (approved 2026-11-03, end of retire v1 phase E).
    raise RuntimeError("d8f0a2c4e6b9 is irreversible: backup table dropped "
                       "(campaign_candidate_breakdown_backup, by b4d6f8a0c2e5) - the v1-shaped breakdowns are gone.")
