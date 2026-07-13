"""jd_pipeline_missing_objects

Revision ID: f6a1c9e34d02
Revises: a92163422dba
Create Date: 2026-07-11 00:00:00.000000

Patches drift between the DB (stamped at a92163422dba) and the models in
app/models/async_tasks.py and app/models/skills.py. Most of c8f2a4d6e910
("jd_processing_pipeline") was already applied to this database out of band
before that migration was written — this only creates what's still missing:
document_processing_stage_executions, celery_task_log.jd_id, jd_skills'
confidence/match_tier columns, jd_unknown_skills, the job_descriptions
content_hash index, and the active embedding_model_versions seed row.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f6a1c9e34d02'
down_revision: Union[str, Sequence[str], None] = 'a92163422dba'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SEED_EMBEDDING_MODEL_VERSION_ID = "a1b2c3d4-e5f6-4a11-8c9d-0e1f2a3b4c5d"


def upgrade() -> None:
    """Upgrade schema."""

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

    op.add_column('celery_task_log', sa.Column('jd_id', sa.UUID(), nullable=True))
    op.create_foreign_key(
        'fk_celery_task_log_jd_id', 'celery_task_log', 'job_descriptions', ['jd_id'], ['id'],
    )

    op.add_column('jd_skills', sa.Column('confidence', sa.Float(), nullable=True))
    op.add_column('jd_skills', sa.Column('match_tier', sa.Text(), nullable=False, server_default='EXACT'))
    op.alter_column('jd_skills', 'match_tier', server_default=None)

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

    op.create_index('ix_job_descriptions_content_hash', 'job_descriptions', ['content_hash'])

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

    op.drop_column('jd_skills', 'match_tier')
    op.drop_column('jd_skills', 'confidence')

    op.drop_constraint('fk_celery_task_log_jd_id', 'celery_task_log', type_='foreignkey')
    op.drop_column('celery_task_log', 'jd_id')

    op.drop_index('ix_stage_exec_document', table_name='document_processing_stage_executions')
    op.drop_index('ix_stage_exec_task_id', table_name='document_processing_stage_executions')
    op.drop_table('document_processing_stage_executions')

    sa.Enum(name='stage_execution_status_enum').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='processing_stage_enum').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='document_type_enum').drop(op.get_bind(), checkfirst=True)
