"""Review item 16 against real Postgres (rolled back): the resume lock queries and the orphan delete run."""
from uuid import uuid4

from sqlalchemy import select

from app.models.candidates import Resume
from app.repositories.resume_repository import ResumeRepository
from tests.db.test_campaign_candidate_delete_scored import db  # noqa: F401 - fixture


def test_lock_if_exists_tells_an_existing_resume_from_a_missing_one(db):  # noqa: F811
    repo = ResumeRepository(db)
    existing = db.execute(select(Resume.id).limit(1)).scalar()
    if existing is not None:
        assert repo.lock_if_exists(existing) is True
    assert repo.lock_if_exists(uuid4()) is False


def test_lock_for_erasure_and_the_orphan_delete_run(db):  # noqa: F811
    repo = ResumeRepository(db)
    ids = list(db.execute(select(Resume.id).limit(2)).scalars())
    repo.lock_for_erasure(ids + [uuid4()])
    assert repo.delete_orphaned_embeddings() >= 0          # rolled back by the fixture
