"""jd_processing_pipeline

Revision ID: c8f2a4d6e910
Revises: a41e892f4a72
Create Date: 2026-07-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import pgvector.sqlalchemy
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c8f2a4d6e910'
down_revision: Union[str, Sequence[str], None] = 'a41e892f4a72'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SEED_EMBEDDING_MODEL_VERSION_ID = "a1b2c3d4-e5f6-4a11-8c9d-0e1f2a3b4c5d"


def upgrade() -> None:
    """Upgrade schema."""

    # ── Async processing: generic per-stage execution tracking ────────────────
    op.create_table(
        'document_processing_stage_executions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('task_id', sa.String(length=255), nullable=False),
        sa.Column('document_type', sa.Enum('JD', 'RESUME', name='document_type_enum'), nullable=False),
        sa.Column('document_id', sa.UUID(), nullable=True),
        sa.Column('stage', sa.Enum(
            'VALIDATION', 'STORAGE', 'TEXT_EXTRACTION', 'TEXT_CLEANING',
            'AI_EXTRACTION', 'JSON_VALIDATION', 'SKILL_NORMALIZATION',
            'EMBEDDING_GENERATION', 'PERSISTENCE',
            name='processing_stage_enum',
        ), nullable=False),
        sa.Column('status', sa.Enum(
            'PENDING', 'RUNNING', 'SUCCESS', 'FAILED', 'SKIPPED',
            name='stage_execution_status_enum',
        ), nullable=False),
        sa.Column('attempt_number', sa.SmallInteger(), nullable=False, server_default='1'),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('duration_ms', sa.Integer(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('task_id', 'stage', 'attempt_number', name='uq_stage_exec_task_stage_attempt'),
    )
    op.create_index('ix_stage_exec_task_id', 'document_processing_stage_executions', ['task_id'])
    op.create_index('ix_stage_exec_document', 'document_processing_stage_executions', ['document_type', 'document_id'])

    # ── Correlate a Celery task back to the JD it eventually persists ─────────
    op.add_column('celery_task_log', sa.Column('jd_id', sa.UUID(), nullable=True))
    op.create_foreign_key(
        'fk_celery_task_log_jd_id', 'celery_task_log', 'job_descriptions', ['jd_id'], ['id'],
    )

    # ── Reconcile skill ontology schema with app/models/skills.py ─────────────
    op.rename_table('skills', 'skill_ontology')

    op.drop_column('skill_ontology', 'aliases')
    op.add_column('skill_ontology', sa.Column('aliases', postgresql.ARRAY(sa.String()), nullable=True))
    op.add_column('skill_ontology', sa.Column('embedding', pgvector.sqlalchemy.vector.VECTOR(dim=384), nullable=True))
    op.add_column('skill_ontology', sa.Column('confidence', sa.Text(), nullable=False, server_default='unverified'))
    op.add_column('skill_ontology', sa.Column('source', sa.Text(), nullable=True))
    op.add_column('skill_ontology', sa.Column('embedding_updated_at', sa.DateTime(timezone=True), nullable=True))

    op.create_index('idx_skill_ontology_aliases', 'skill_ontology', ['aliases'], postgresql_using='gin')
    op.create_index(
        'idx_skill_ontology_embedding', 'skill_ontology', ['embedding'],
        postgresql_using='ivfflat', postgresql_ops={'embedding': 'vector_cosine_ops'},
    )

    op.create_table(
        'unknown_skills',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('raw_text', sa.Text(), nullable=False),
        sa.Column('frequency', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('first_seen', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('last_seen', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('normalized_key', sa.String(length=200), nullable=True),
        sa.Column('skill_suggestion_id', sa.UUID(), nullable=True),
        sa.Column('status', sa.Text(), nullable=False, server_default='PENDING'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['skill_suggestion_id'], ['skill_suggestions.id'], name='fk_unknown_skill_suggestion_id'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('raw_text'),
    )

    op.add_column('skill_suggestions', sa.Column('unknown_skill_id', sa.UUID(), nullable=True))
    op.add_column('skill_suggestions', sa.Column('suggested_parent_skill_id', sa.UUID(), nullable=True))
    op.create_foreign_key(
        'fk_skill_suggestions_unknown_skill_id', 'skill_suggestions', 'unknown_skills', ['unknown_skill_id'], ['id'],
    )
    op.create_foreign_key(
        'fk_skill_suggestions_suggested_parent_skill_id', 'skill_suggestions', 'skill_ontology',
        ['suggested_parent_skill_id'], ['id'],
    )

    op.create_table(
        'jd_skills',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('jd_id', sa.UUID(), nullable=False),
        sa.Column('canonical_skill_id', sa.UUID(), nullable=False),
        sa.Column('mandatory', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('weight', sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column('confidence', sa.Float(), nullable=True),
        sa.Column('match_tier', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['jd_id'], ['job_descriptions.id'], ),
        sa.ForeignKeyConstraint(['canonical_skill_id'], ['skill_ontology.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('jd_id', 'canonical_skill_id'),
    )

    # ── Per-JD traceability for otherwise-globally-deduped unknown skills ─────
    op.create_table(
        'jd_unknown_skills',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('jd_id', sa.UUID(), nullable=False),
        sa.Column('unknown_skill_id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['jd_id'], ['job_descriptions.id'], ),
        sa.ForeignKeyConstraint(['unknown_skill_id'], ['unknown_skills.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('jd_id', 'unknown_skill_id'),
    )

    # ── Recommended: index content_hash now that the dedup race window is wider ─
    op.create_index('ix_job_descriptions_content_hash', 'job_descriptions', ['content_hash'])

    # ── Seed the active embedding model version the Persistence stage requires ─
    op.execute(
        f"""
        INSERT INTO embedding_model_versions
            (id, model_name, model_version, vector_dimensions, distance_metric, is_active)
        SELECT '{SEED_EMBEDDING_MODEL_VERSION_ID}', 'all-MiniLM-L6-v2', '1', 384, 'cosine', true
        WHERE NOT EXISTS (
            SELECT 1 FROM embedding_model_versions WHERE model_name = 'all-MiniLM-L6-v2' AND is_active = true
        )
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        f"DELETE FROM embedding_model_versions WHERE id = '{SEED_EMBEDDING_MODEL_VERSION_ID}'"
    )
    op.drop_index('ix_job_descriptions_content_hash', table_name='job_descriptions')

    op.drop_table('jd_unknown_skills')
    op.drop_table('jd_skills')

    op.drop_constraint('fk_skill_suggestions_suggested_parent_skill_id', 'skill_suggestions', type_='foreignkey')
    op.drop_constraint('fk_skill_suggestions_unknown_skill_id', 'skill_suggestions', type_='foreignkey')
    op.drop_column('skill_suggestions', 'suggested_parent_skill_id')
    op.drop_column('skill_suggestions', 'unknown_skill_id')

    op.drop_table('unknown_skills')

    op.drop_index('idx_skill_ontology_embedding', table_name='skill_ontology')
    op.drop_index('idx_skill_ontology_aliases', table_name='skill_ontology')
    op.drop_column('skill_ontology', 'embedding_updated_at')
    op.drop_column('skill_ontology', 'source')
    op.drop_column('skill_ontology', 'confidence')
    op.drop_column('skill_ontology', 'embedding')
    op.drop_column('skill_ontology', 'aliases')
    op.add_column('skill_ontology', sa.Column('aliases', postgresql.JSONB(astext_type=sa.Text()), nullable=True))

    op.rename_table('skill_ontology', 'skills')

    op.drop_constraint('fk_celery_task_log_jd_id', 'celery_task_log', type_='foreignkey')
    op.drop_column('celery_task_log', 'jd_id')

    op.drop_index('ix_stage_exec_document', table_name='document_processing_stage_executions')
    op.drop_index('ix_stage_exec_task_id', table_name='document_processing_stage_executions')
    op.drop_table('document_processing_stage_executions')

    sa.Enum(name='stage_execution_status_enum').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='processing_stage_enum').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='document_type_enum').drop(op.get_bind(), checkfirst=True)
