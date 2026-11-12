"""embeddings: one per (item, model version), so a re-embed can keep the old ones alongside

Revision ID: e2a4c6d8f0b1
Revises: d0f2a4b6c8e3
Create Date: 2026-11-12

Settings phase 4 (approved 2026-11-12, exactly these statements):
  - jd_embeddings: UNIQUE (jd_id) -> UNIQUE (jd_id, embedding_model_version_id) - a JD can hold the active
    model's embedding and a new model's while a re-embed runs;
  - resume_embeddings: UNIQUE (resume_id, embedding_model_version_id) - additive; the code already relied on it
    (ResumeRepository.create_resume_embedding) but the database never had it. Dev had no duplicate.

Downgrade restores UNIQUE (jd_id) and refuses, naming the JD, while any JD has embeddings from more than one
model version: it never deletes an embedding.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e2a4c6d8f0b1"
down_revision: Union[str, None] = "d0f2a4b6c8e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE jd_embeddings DROP CONSTRAINT jd_embeddings_jd_id_key")
    op.execute("ALTER TABLE jd_embeddings ADD CONSTRAINT uq_jd_embeddings_jd_model_version "
               "UNIQUE (jd_id, embedding_model_version_id)")
    op.execute("ALTER TABLE resume_embeddings ADD CONSTRAINT uq_resume_embeddings_resume_model_version "
               "UNIQUE (resume_id, embedding_model_version_id)")


def downgrade() -> None:
    conflicts = op.get_bind().execute(sa.text(
        "SELECT e.jd_id, j.title, count(DISTINCT e.embedding_model_version_id) AS versions "
        "FROM jd_embeddings e LEFT JOIN job_descriptions j ON j.id = e.jd_id "
        "GROUP BY e.jd_id, j.title HAVING count(DISTINCT e.embedding_model_version_id) > 1 ORDER BY j.title"
    )).all()
    if conflicts:
        named = "; ".join(f"JD '{title or '(untitled)'}' ({jd_id}): {versions} model versions"
                          for jd_id, title, versions in conflicts)
        raise RuntimeError(
            "Can't go back to one embedding per JD: these JDs have embeddings from more than one model version - "
            f"{named}. A downgrade never deletes embeddings: remove the ones you don't need first, then retry.")
    op.execute("ALTER TABLE resume_embeddings DROP CONSTRAINT uq_resume_embeddings_resume_model_version")
    op.execute("ALTER TABLE jd_embeddings DROP CONSTRAINT uq_jd_embeddings_jd_model_version")
    op.execute("ALTER TABLE jd_embeddings ADD CONSTRAINT jd_embeddings_jd_id_key UNIQUE (jd_id)")
