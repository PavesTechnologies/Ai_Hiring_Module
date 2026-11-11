"""drop columns no code ever writes or reads

Revision ID: d4f6b8c0e2a5
Revises: c3e5a7b9d1f4
Create Date: 2026-11-11

Approved 2026-11-11 (pre-deployment dead-code audit): of the 19 unread columns, drop those that are also
never written - no ORM write, no raw SQL, no database default fills them; all null in dev (skill_suggestions
has no rows; nothing inserts into it):
  resumes.parse_duration_ms; search_queries.query_embedding_hash, top_score;
  skill_suggestions.raw_skill_text, suggested_canonical_name, suggested_parent_skill_id, reviewed_by,
  reviewed_at; unknown_skills.skill_suggestion_id.
Kept: candidate_consent.revocation_reason / revoked_at, encryption_keys.rotated_at and
retention_policies.last_enforced_at (planned features); ai_provider_model.provider_config_id (the foreign
key its relationship writes); resume_provisional_skill_scans.scanned_at and user_oauth_tokens.connected_at
(NOT NULL, filled by now() on every insert). The api_rate_limit_buckets columns went with the table in
c3e5a7b9d1f4.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4f6b8c0e2a5"
down_revision: Union[str, None] = "c3e5a7b9d1f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("resumes", "parse_duration_ms")
    op.drop_column("search_queries", "query_embedding_hash")
    op.drop_column("search_queries", "top_score")
    op.drop_constraint("fk_unknown_skill_suggestion_id", "unknown_skills", type_="foreignkey")
    op.drop_column("unknown_skills", "skill_suggestion_id")
    op.drop_constraint("skill_suggestions_suggested_parent_skill_id_fkey", "skill_suggestions", type_="foreignkey")
    op.drop_constraint("skill_suggestions_reviewed_by_fkey", "skill_suggestions", type_="foreignkey")
    for column in ("raw_skill_text", "suggested_canonical_name", "suggested_parent_skill_id", "reviewed_by",
                   "reviewed_at"):
        op.drop_column("skill_suggestions", column)


def downgrade() -> None:
    op.add_column("skill_suggestions", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("skill_suggestions", sa.Column("reviewed_by", sa.String(length=255), nullable=True))
    op.add_column("skill_suggestions", sa.Column("suggested_parent_skill_id", sa.UUID(), nullable=True))
    op.add_column("skill_suggestions", sa.Column("suggested_canonical_name", sa.String(length=100), nullable=True))
    # NOT NULL originally; the default only lets the column come back on a table that has rows.
    op.add_column("skill_suggestions", sa.Column("raw_skill_text", sa.String(length=200), nullable=False,
                                                 server_default=sa.text("''")))
    op.alter_column("skill_suggestions", "raw_skill_text", server_default=None)
    op.create_foreign_key("skill_suggestions_reviewed_by_fkey", "skill_suggestions", "users",
                          ["reviewed_by"], ["id"])
    op.create_foreign_key("skill_suggestions_suggested_parent_skill_id_fkey", "skill_suggestions", "skill_ontology",
                          ["suggested_parent_skill_id"], ["id"])
    op.add_column("unknown_skills", sa.Column("skill_suggestion_id", sa.UUID(), nullable=True))
    op.create_foreign_key("fk_unknown_skill_suggestion_id", "unknown_skills", "skill_suggestions",
                          ["skill_suggestion_id"], ["id"])
    op.add_column("search_queries", sa.Column("top_score", sa.Numeric(precision=7, scale=6), nullable=True))
    op.add_column("search_queries", sa.Column("query_embedding_hash", sa.String(length=64), nullable=True))
    op.add_column("resumes", sa.Column("parse_duration_ms", sa.Integer(), nullable=True))
