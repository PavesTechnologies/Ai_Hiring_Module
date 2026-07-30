"""resume_embeddings + celery_task_log concurrency-safety unique constraints

Production-readiness audit fix for EMBED_RESUME (M08-E01): adds a unique
constraint on resume_embeddings(resume_id, embedding_model_version_id) so
concurrent workers (broker redelivery of a crashed RUNNING task, a manual
re-trigger, etc.) can never insert two embedding rows for the same
resume+model pair, and a partial unique index on
celery_task_log(idempotency_key) — excluding NULL, since idempotency_key is
optional and most task types never set it — so the same class of race can
never double-enqueue a task that keys its idempotency on this column.

Revision ID: d4b7f1c8a3e6
Revises: c9e2a5f8b1d4
Create Date: 2026-07-28
"""
from alembic import op
import sqlalchemy as sa

revision = "d4b7f1c8a3e6"
down_revision = "c9e2a5f8b1d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_resume_embeddings_resume_model_version",
        "resume_embeddings",
        ["resume_id", "embedding_model_version_id"],
    )
    op.create_index(
        "uq_celery_task_log_idempotency_key",
        "celery_task_log",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_celery_task_log_idempotency_key", table_name="celery_task_log")
    op.drop_constraint("uq_resume_embeddings_resume_model_version", "resume_embeddings", type_="unique")
