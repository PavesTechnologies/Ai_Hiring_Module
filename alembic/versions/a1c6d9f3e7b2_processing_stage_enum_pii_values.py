"""processing_stage_enum add PII_DETECTION and PII_REDACTION

Resume PII pipeline (ResumeProcessingPipeline) stage-tracks PII_DETECTION
and PII_REDACTION via StageExecutionService, using ProcessingStage
(app/models/async_tasks.py) - present in that Python enum but never applied
to the native processing_stage_enum DB type (same "model enum updated,
DB-side ALTER TYPE migration never written" drift as the audit-enum fixes
elsewhere in this history, e.g. f3a6c9d1b7e2/e6f3b9a1c5d7). Every resume
upload hits this the moment stage tracking reaches PII_DETECTION.

Revision ID: a1c6d9f3e7b2
Revises: d3f7b9c2a1e5
Create Date: 2026-07-29
"""
from alembic import op

revision = "a1c6d9f3e7b2"
down_revision = "d3f7b9c2a1e5"
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction block.
transactional_ddl = False


def upgrade() -> None:
    op.execute("ALTER TYPE processing_stage_enum ADD VALUE IF NOT EXISTS 'PII_DETECTION'")
    op.execute("ALTER TYPE processing_stage_enum ADD VALUE IF NOT EXISTS 'PII_REDACTION'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type; leaving these in place is harmless.
    pass
