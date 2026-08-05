"""resume_embeddings ivfflat index

Embedding Storage Dashboard (M08): resume_embeddings.embedding has had no
index at all since it was created — ResumeRepository.get_cosine_similarity
runs a plain sequential-scan .cosine_distance() filter. Adds an ivfflat
index mirroring skill_ontology's existing embedding index exactly
(same postgresql_using="ivfflat" / vector_cosine_ops, no explicit "lists"
override), giving the new REINDEX_IVFFLAT task and dashboard health check
an actual index to report on / rebuild.

Revision ID: b3e7a1c9d5f2
Revises: a5f3d8c1b6e4
Create Date: 2026-08-03
"""
from alembic import op

revision = "b3e7a1c9d5f2"
down_revision = "a5f3d8c1b6e4"
branch_labels = None
depends_on = None

RESUME_EMBEDDINGS_IVFFLAT_INDEX = "idx_resume_embeddings_embedding"


def upgrade() -> None:
    op.create_index(
        RESUME_EMBEDDINGS_IVFFLAT_INDEX,
        "resume_embeddings",
        ["embedding"],
        unique=False,
        postgresql_using="ivfflat",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index(RESUME_EMBEDDINGS_IVFFLAT_INDEX, table_name="resume_embeddings")
