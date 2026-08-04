"""placeholder for missing revision (merge)

alembic_version was found stamped at 'a5f3d8c1b6e4' - a revision referenced
as b3e7a1c9d5f2's down_revision but that does not exist as a migration file
anywhere in this repo (same class of issue as a1c6d9f3e7b2's and
265912f5590a's own "placeholder for missing revision" fixes: someone
applied schema changes directly against the shared dev DB, stamped
alembic_version by hand, and never committed the migration file).

At the point this was stamped, every one of the following independently-
headed branches had already had its own real migration applied live
(verified against the actual live schema - every column/constraint/index/
enum value each one adds was already present, with no unexplained schema
beyond them):
  - 3ae81a38abdc (resumes.original_filename/file_size_bytes)
  - d6b8e3a1f4c9 (email_trigger_event_enum UPLOAD_PERMANENTLY_FAILED)
  - d4b7f1c8a3e6 (resume_embeddings/celery_task_log concurrency constraints)
  - e2c8a4f6b9d1 (campaign_candidates.semantic_score_breakdown + audit enum)
  - 3e7800c51995 (prior merge - itself a no-op)
  - b4d8e1f6a3c7 (bulk_upload_job_files.task_id)
  - 90b05f9f2aa1 (audit_action_type_enum CANDIDATE_DATA_ERASED)

This is therefore authored as a merge of those 7 heads into the single
missing revision id the DB is already stamped with - a no-op (the schema
changes already happened), not a guess at a real change. b3e7a1c9d5f2
(resume_embeddings ivfflat index) continues to chain on top of this
exactly as before.

Revision ID: a5f3d8c1b6e4
Revises: 3ae81a38abdc, d6b8e3a1f4c9, d4b7f1c8a3e6, e2c8a4f6b9d1, 3e7800c51995, b4d8e1f6a3c7, 90b05f9f2aa1
Create Date: 2026-08-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a5f3d8c1b6e4'
down_revision: Union[str, Sequence[str], None] = (
    '3ae81a38abdc',
    'd6b8e3a1f4c9',
    'd4b7f1c8a3e6',
    'e2c8a4f6b9d1',
    '3e7800c51995',
    'b4d8e1f6a3c7',
    '90b05f9f2aa1',
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
