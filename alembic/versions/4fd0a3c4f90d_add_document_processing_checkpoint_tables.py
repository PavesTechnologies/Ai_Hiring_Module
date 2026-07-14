"""add document processing checkpoint tables

Revision ID: 4fd0a3c4f90d
Revises: c71678d36109
Create Date: 2026-07-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '4fd0a3c4f90d'
down_revision: Union[str, Sequence[str], None] = 'c71678d36109'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'document_processing_checkpoints',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('task_id', sa.String(length=255), nullable=False),
        sa.Column('document_type', sa.Enum('JD', 'RESUME', name='document_type_enum'), nullable=False),
        sa.Column('failed_at_stage', sa.Enum(
            'VALIDATION', 'STORAGE', 'TEXT_EXTRACTION', 'TEXT_CLEANING',
            'AI_EXTRACTION', 'JSON_VALIDATION', 'SKILL_NORMALIZATION',
            'EMBEDDING_GENERATION', 'PERSISTENCE',
            name='processing_stage_enum',
        ), nullable=True),
        sa.Column('context_data', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('task_id'),
    )

    op.create_table(
        'stage_failure_logs',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('task_id', sa.String(length=255), nullable=False),
        sa.Column('stage', sa.Enum(
            'VALIDATION', 'STORAGE', 'TEXT_EXTRACTION', 'TEXT_CLEANING',
            'AI_EXTRACTION', 'JSON_VALIDATION', 'SKILL_NORMALIZATION',
            'EMBEDDING_GENERATION', 'PERSISTENCE',
            name='processing_stage_enum',
        ), nullable=False),
        sa.Column('attempt_number', sa.SmallInteger(), nullable=False),
        sa.Column('exception_type', sa.String(length=255), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('classification', sa.Enum('TRANSIENT', 'PERMANENT', 'UNKNOWN', name='failure_classification_enum'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('stage_failure_logs')
    op.drop_table('document_processing_checkpoints')
    sa.Enum(name='failure_classification_enum').drop(op.get_bind(), checkfirst=True)
