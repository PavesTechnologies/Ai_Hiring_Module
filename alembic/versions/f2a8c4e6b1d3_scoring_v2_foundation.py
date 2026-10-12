"""Scoring v2 foundation (Phase 1, shadow mode)

- hiring_campaigns.scoring_v2_enabled (default false): v2 drives decisions
  only on campaigns where it is switched on; elsewhere it is computed and
  stored alongside v1 without acting.
- skill_senses, skill_scan_terms: catalogue data for the text scan.
- resume_skill_evidence, resume_parse_cache, core_skill_confirmations:
  v2 skill detection.
- candidate_scoring_results: v1/v2 scores with their config snapshot.
- resume_parse_versions: previous parses kept on re-parse.

Additive only - no existing table or row changes.

Revision ID: f2a8c4e6b1d3
Revises: e7c3a1d5b9f2
Create Date: 2026-10-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f2a8c4e6b1d3"
down_revision: Union[str, Sequence[str], None] = "e7c3a1d5b9f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)


def _id():
    return sa.Column("id", UUID, primary_key=True)


def _created_at():
    return sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.add_column(
        "hiring_campaigns",
        sa.Column("scoring_v2_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    op.create_table(
        "skill_senses",
        _id(),
        sa.Column("canonical_skill_id", UUID, sa.ForeignKey("skill_ontology.id"), nullable=False),
        sa.Column("sense_key", sa.String(50), nullable=False),
        sa.Column("label", sa.String(150), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("cue_words", postgresql.ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("window_lines", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        _created_at(),
        sa.UniqueConstraint("canonical_skill_id", "sense_key", name="uq_skill_senses_skill_sense"),
    )
    op.create_index("ix_skill_senses_canonical_skill_id", "skill_senses", ["canonical_skill_id"])

    op.create_table(
        "skill_scan_terms",
        _id(),
        sa.Column("term_key", sa.String(100), nullable=False, unique=True),
        sa.Column("policy", sa.String(20), nullable=False),
        sa.Column("cue_words", postgresql.ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("note", sa.Text(), nullable=True),
        _created_at(),
    )

    op.create_table(
        "resume_skill_evidence",
        _id(),
        sa.Column("resume_id", UUID, sa.ForeignKey("resumes.id"), nullable=False),
        sa.Column("canonical_skill_id", UUID, sa.ForeignKey("skill_ontology.id"), nullable=False),
        sa.Column("sense", sa.String(50), nullable=True),
        sa.Column("sense_resolved", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("sources", postgresql.ARRAY(sa.String(20)), nullable=False),
        sa.Column("best_source", sa.String(20), nullable=False),
        sa.Column("match_tier", sa.String(30), nullable=False),
        sa.Column("recognition", sa.Numeric(4, 3), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("text_hash", sa.String(64), nullable=False),
        sa.Column("detection_version", sa.String(20), nullable=False),
        sa.Column("parse_provider", sa.String(50), nullable=True),
        sa.Column("parse_model", sa.String(150), nullable=True),
        sa.Column("parse_used_fallback", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        _created_at(),
    )
    op.create_index("ix_resume_skill_evidence_resume_id", "resume_skill_evidence", ["resume_id"])

    op.create_table(
        "resume_parse_cache",
        _id(),
        sa.Column("cache_key", sa.String(64), nullable=False, unique=True),
        sa.Column("text_hash", sa.String(64), nullable=False),
        sa.Column("prompt_hash", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(50), nullable=True),
        sa.Column("model", sa.String(150), nullable=True),
        sa.Column("detection_version", sa.String(20), nullable=False),
        sa.Column("raw_extraction", postgresql.JSONB(), nullable=False),
        sa.Column("hit_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_hit_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
    )

    op.create_table(
        "core_skill_confirmations",
        _id(),
        sa.Column("cache_key", sa.String(64), nullable=False, unique=True),
        sa.Column("resume_id", UUID, sa.ForeignKey("resumes.id"), nullable=False),
        sa.Column("jd_id", UUID, sa.ForeignKey("job_descriptions.id"), nullable=False),
        sa.Column("text_hash", sa.String(64), nullable=False),
        sa.Column("core_set_hash", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("detection_version", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(50), nullable=True),
        sa.Column("model", sa.String(150), nullable=True),
        sa.Column("used_fallback", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("results", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        _created_at(),
    )
    op.create_index("ix_core_skill_confirmations_resume_jd", "core_skill_confirmations", ["resume_id", "jd_id"])

    op.create_table(
        "candidate_scoring_results",
        _id(),
        sa.Column("campaign_candidate_id", UUID, sa.ForeignKey("campaign_candidates.id"), nullable=False),
        sa.Column("scoring_version", sa.String(10), nullable=False),
        sa.Column("score", sa.Numeric(5, 2), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=True),
        sa.Column("outcome", sa.String(30), nullable=True),
        sa.Column("breakdown", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("scoring_config_version", sa.String(64), nullable=False),
        sa.Column("config_snapshot", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("acted_on", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        _created_at(),
    )
    op.create_index(
        "ix_candidate_scoring_results_cc_version", "candidate_scoring_results",
        ["campaign_candidate_id", "scoring_version", "created_at"],
    )

    op.create_table(
        "resume_parse_versions",
        _id(),
        sa.Column("resume_id", UUID, sa.ForeignKey("resumes.id"), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("parsed_json", postgresql.JSONB(), nullable=True),
        sa.Column("skills_snapshot", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("evidence_snapshot", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("diff", postgresql.JSONB(), nullable=True),
        sa.Column("triggered_by", sa.String(255), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        _created_at(),
        sa.UniqueConstraint("resume_id", "version_no", name="uq_resume_parse_versions_resume_version"),
    )


def downgrade() -> None:
    op.drop_table("resume_parse_versions")
    op.drop_index("ix_candidate_scoring_results_cc_version", table_name="candidate_scoring_results")
    op.drop_table("candidate_scoring_results")
    op.drop_index("ix_core_skill_confirmations_resume_jd", table_name="core_skill_confirmations")
    op.drop_table("core_skill_confirmations")
    op.drop_table("resume_parse_cache")
    op.drop_index("ix_resume_skill_evidence_resume_id", table_name="resume_skill_evidence")
    op.drop_table("resume_skill_evidence")
    op.drop_table("skill_scan_terms")
    op.drop_index("ix_skill_senses_canonical_skill_id", table_name="skill_senses")
    op.drop_table("skill_senses")
    op.drop_column("hiring_campaigns", "scoring_v2_enabled")
