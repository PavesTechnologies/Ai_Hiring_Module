"""Against Postgres (rolled back): with a second model's embeddings beside the active one's, every lookup and the
semantic similarity use the active version's (settings phase 4); the new constraints hold."""
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models.embeddings import EmbeddingModelVersion, ResumeEmbedding
from app.models.jd.job_descriptions import JDEmbedding
from app.repositories.jd_repository import JDRepository
from app.repositories.resume_repository import ResumeRepository
from tests.db.test_campaign_candidate_delete_scored import db  # noqa: F401 - fixture


@pytest.fixture
def two_versions(db):  # noqa: F811
    resume_row = db.query(ResumeEmbedding).first()
    jd_row = db.query(JDEmbedding).first()
    if resume_row is None or jd_row is None:
        pytest.skip("needs a resume and a JD embedding")
    new = EmbeddingModelVersion(id=uuid.uuid4(), model_name=f"next-model-{uuid.uuid4().hex[:6]}", model_version="v1",
                                vector_dimensions=384, distance_metric="cosine", is_active=False)
    db.add(new)
    db.flush()
    flipped = [-x for x in resume_row.embedding]                      # the opposite direction: similarity -1
    db.add(ResumeEmbedding(resume_id=resume_row.resume_id, candidate_id=resume_row.candidate_id, embedding=flipped,
                           embedding_model_version_id=new.id, input_text_hash="next", is_anonymized=True,
                           is_talent_pool_eligible=True))
    db.add(JDEmbedding(jd_id=jd_row.jd_id, embedding=list(jd_row.embedding), embedding_model_version_id=new.id,
                       input_text_hash="next", embedding_status="READY"))
    db.flush()
    return resume_row, jd_row, new


def test_lookups_return_the_active_versions_embedding(db, two_versions):  # noqa: F811
    resume_row, jd_row, new = two_versions
    assert ResumeRepository(db).get_embedding(resume_row.resume_id).id == resume_row.id
    assert JDRepository(db).get_embedding_by_jd_id(jd_row.jd_id).id == jd_row.id
    assert JDRepository(db).get_embedding_by_jd_id(jd_row.jd_id, new.id).embedding_model_version_id == new.id


def test_similarity_uses_the_active_pair_only(db, two_versions):  # noqa: F811
    resume_row, jd_row, _ = two_versions
    expected = db.execute(text(
        "select 1 - (r.embedding <=> j.embedding) from resume_embeddings r, jd_embeddings j "
        "where r.id = :r and j.id = :j"), {"r": resume_row.id, "j": jd_row.id}).scalar()
    value = ResumeRepository(db).compute_semantic_similarity(resume_row.resume_id, jd_row.jd_id)
    assert value == pytest.approx(float(expected))


def test_one_embedding_per_item_and_model_version(db, two_versions):  # noqa: F811
    resume_row, jd_row, new = two_versions
    with pytest.raises(IntegrityError):
        with db.begin_nested():
            db.add(JDEmbedding(jd_id=jd_row.jd_id, embedding=list(jd_row.embedding), embedding_model_version_id=new.id,
                               input_text_hash="again", embedding_status="READY"))
            db.flush()
    with pytest.raises(IntegrityError):
        with db.begin_nested():
            db.add(ResumeEmbedding(resume_id=resume_row.resume_id, candidate_id=resume_row.candidate_id,
                                   embedding=list(resume_row.embedding), embedding_model_version_id=new.id,
                                   input_text_hash="again", is_anonymized=True, is_talent_pool_eligible=True))
            db.flush()
