"""candidate_outcome_overrides: a person moved a candidate against the system's screening outcome

Revision ID: b8d0f2a4c6e1
Revises: a7c9e1b3d5f6
Create Date: 2026-10-30

Phase 4, recruiter overrides: one row per move against the screening outcome (a PASS rejected or
put on hold, a REJECT brought back) or resolving a HOLD, with the result it contradicted and a
reason code. Additive: a new table.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b8d0f2a4c6e1"
down_revision: Union[str, Sequence[str], None] = "a7c9e1b3d5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STAGE = postgresql.ENUM(name="pipeline_stage_enum", create_type=False)


def upgrade() -> None:
    op.create_table(
        "candidate_outcome_overrides",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("campaign_candidate_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("campaign_candidates.id"), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("hiring_campaigns.id"), nullable=False),
        sa.Column("scoring_result_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("candidate_scoring_results.id"), nullable=True),
        sa.Column("scoring_version", sa.String(10), nullable=True),
        sa.Column("scoring_config_version", sa.String(64), nullable=True),
        sa.Column("system_outcome", sa.String(20), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("direction", sa.String(10), nullable=False),
        sa.Column("from_stage", STAGE, nullable=False),
        sa.Column("to_stage", STAGE, nullable=False),
        sa.Column("reason_code", sa.String(40), nullable=False),
        sa.Column("failed_checks", postgresql.JSONB(), nullable=True),
        sa.Column("missing_core_skills", postgresql.JSONB(), nullable=True),
        sa.Column("profile_template", sa.String(20), nullable=True),
        sa.Column("actor_id", sa.String(255), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("actor_role", sa.String(50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_outcome_overrides_campaign_created", "candidate_outcome_overrides",
                    ["campaign_id", "created_at"])
    op.create_index("idx_outcome_overrides_campaign_candidate", "candidate_outcome_overrides",
                    ["campaign_candidate_id"])


def downgrade() -> None:
    op.drop_index("idx_outcome_overrides_campaign_candidate", table_name="candidate_outcome_overrides")
    op.drop_index("idx_outcome_overrides_campaign_created", table_name="candidate_outcome_overrides")
    op.drop_table("candidate_outcome_overrides")
