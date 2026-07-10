"""convert users.id and all referencing user-id FK columns from uuid to varchar

Revision ID: c7d8e9f0a1b2
Revises: a41e892f4a72
Create Date: 2026-07-02

AIRS now sources caller identity from an external UMS, whose "user_id" claim
is a plain numeric/string id (e.g. "5100029"), not a UUID. users.id and every
column that stores a reference to it need to be plain strings to hold that
value directly. Postgres requires the referencing column type to match the
referenced column type for a foreign key, so users.id and all 15 FK columns
across 13 tables must change together in one migration:

  audit_log.actor_id                            bulk_upload_jobs.uploaded_by
  campaign_candidate_stage_history.changed_by    campaign_candidates.hr_override_by
  candidate_rejections.rejected_by               dead_letter_queue.replayed_by
  encryption_keys.created_by                     hiring_campaigns.created_by
  hiring_campaigns.hiring_manager_id             job_descriptions.created_by
  platform_config.updated_by                     prompt_versions.created_by
  resumes.uploaded_by                            search_queries.queried_by
  skill_suggestions.reviewed_by

Existing uuid values are preserved as their text representation via
USING column::text. Downgrade only works if every stored value is still a
valid UUID string (true immediately after this migration, not guaranteed
once real non-UUID ids like "5100029" have been written).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7d8e9f0a1b2'
down_revision: Union[str, None] = 'a41e892f4a72'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (constraint_name, table, column)
_FKS = [
    ('audit_log_actor_id_fkey', 'audit_log', 'actor_id'),
    ('bulk_upload_jobs_uploaded_by_fkey', 'bulk_upload_jobs', 'uploaded_by'),
    ('campaign_candidate_stage_history_changed_by_fkey', 'campaign_candidate_stage_history', 'changed_by'),
    ('campaign_candidates_hr_override_by_fkey', 'campaign_candidates', 'hr_override_by'),
    ('candidate_rejections_rejected_by_fkey', 'candidate_rejections', 'rejected_by'),
    ('dead_letter_queue_replayed_by_fkey', 'dead_letter_queue', 'replayed_by'),
    ('encryption_keys_created_by_fkey', 'encryption_keys', 'created_by'),
    ('hiring_campaigns_created_by_fkey', 'hiring_campaigns', 'created_by'),
    ('hiring_campaigns_hiring_manager_id_fkey', 'hiring_campaigns', 'hiring_manager_id'),
    ('job_descriptions_created_by_fkey', 'job_descriptions', 'created_by'),
    ('platform_config_updated_by_fkey', 'platform_config', 'updated_by'),
    ('prompt_versions_created_by_fkey', 'prompt_versions', 'created_by'),
    ('resumes_uploaded_by_fkey', 'resumes', 'uploaded_by'),
    ('search_queries_queried_by_fkey', 'search_queries', 'queried_by'),
    ('skill_suggestions_reviewed_by_fkey', 'skill_suggestions', 'reviewed_by'),
]


def _drop_fk_if_exists(table: str, constraint_name: str) -> None:
    op.execute(
        f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{constraint_name}"'
    )


def upgrade() -> None:
    for constraint_name, table, _column in _FKS:
        _drop_fk_if_exists(table, constraint_name)

    op.alter_column(
        'users', 'id',
        type_=sa.String(255),
        postgresql_using='id::text',
    )

    for _constraint_name, table, column in _FKS:
        op.alter_column(
            table, column,
            type_=sa.String(255),
            postgresql_using=f'{column}::text',
        )

    for constraint_name, table, column in _FKS:
        op.create_foreign_key(constraint_name, table, 'users', [column], ['id'])


def downgrade() -> None:
    for constraint_name, table, _column in _FKS:
        _drop_fk_if_exists(table, constraint_name)

    op.alter_column(
        'users', 'id',
        type_=sa.dialects.postgresql.UUID(as_uuid=True),
        postgresql_using='id::uuid',
    )

    for _constraint_name, table, column in _FKS:
        op.alter_column(
            table, column,
            type_=sa.dialects.postgresql.UUID(as_uuid=True),
            postgresql_using=f'{column}::uuid',
        )

    for constraint_name, table, column in _FKS:
        op.create_foreign_key(constraint_name, table, 'users', [column], ['id'])
