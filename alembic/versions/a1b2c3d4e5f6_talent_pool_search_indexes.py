"""Talent Pool Normal Search - indexes for the fully database-pushed-down filter/pagination query

M13-E01 S02: TalentPoolService.search_candidates now delegates every filter
(skill AND/OR, name, designation, location, education, campaign, pipeline
stage, experience range, composite-score range) plus pagination/COUNT to
ResumeRepository.search_talent_pool, which runs entirely as SQL against
Postgres - nothing is loaded into Python and filtered there anymore. These
indexes back the specific access patterns that query introduces that no
existing index already covered:

- resumes(candidate_id, created_at DESC) filtered to PARSED - backs the
  ROW_NUMBER() OVER (PARTITION BY candidate_id ORDER BY created_at DESC)
  eligibility pick (each candidate's single most-recent eligible resume),
  and the pre-existing per-candidate resume lookups
  (get_active_by_candidate/get_max_version_number/get_all_versions_by_candidate)
  that already filter by candidate_id without an index today.
- resume_embeddings(resume_id, is_talent_pool_eligible) - backs the
  eligibility join's ON resume_id = resumes.id AND is_talent_pool_eligible =
  true; resume_embeddings has no FK/index on resume_id today.
- campaign_candidates(candidate_id) - backs the campaign_ids/pipeline_stages
  EXISTS(... WHERE candidate_id = ...) filters and the best_composite_score
  MAX(...) correlated subquery, plus the pre-existing
  get_candidate_ids_by_campaign/get_best_composite_scores_by_candidate_ids
  callers - campaign_candidates is currently indexed on (campaign_id,
  composite_score) only, nothing on candidate_id.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "d3a86f21c9e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_resumes_candidate_id_created_at_parsed",
        "resumes",
        ["candidate_id", "created_at"],
        unique=False,
        postgresql_where=sa.text("parse_status = 'PARSED'"),
    )
    op.create_index(
        "ix_resume_embeddings_resume_id_eligible",
        "resume_embeddings",
        ["resume_id", "is_talent_pool_eligible"],
        unique=False,
    )
    op.create_index(
        "ix_campaign_candidates_candidate_id",
        "campaign_candidates",
        ["candidate_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_campaign_candidates_candidate_id", table_name="campaign_candidates")
    op.drop_index("ix_resume_embeddings_resume_id_eligible", table_name="resume_embeddings")
    op.drop_index("ix_resumes_candidate_id_created_at_parsed", table_name="resumes")
