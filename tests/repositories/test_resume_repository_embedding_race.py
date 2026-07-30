from contextlib import contextmanager
from unittest.mock import MagicMock
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from app.repositories.resume_repository import ResumeRepository


@contextmanager
def _reraising_savepoint():
    """
    Stands in for Session.begin_nested(): on a clean body it's a no-op: on
    an exception it does not swallow it, matching real SQLAlchemy behavior
    (a SAVEPOINT rollback happens internally, then the original exception
    still propagates to the caller's except block).
    """
    yield


def _repo_with_mock_db():
    db = MagicMock()
    db.begin_nested.side_effect = lambda: _reraising_savepoint()
    return db, ResumeRepository(db)


def test_create_resume_embedding_returns_new_row_when_no_conflict():
    db, repo = _repo_with_mock_db()
    resume_id, candidate_id, model_version_id = uuid4(), uuid4(), uuid4()

    result, was_created = repo.create_resume_embedding(
        resume_id=resume_id,
        candidate_id=candidate_id,
        embedding=[0.1] * 384,
        embedding_model_version_id=model_version_id,
        input_text_hash="abc123",
        is_anonymized=False,
        is_talent_pool_eligible=False,
    )

    assert was_created is True
    assert result.resume_id == resume_id
    assert result.embedding_model_version_id == model_version_id
    # T4: is_anonymized/is_talent_pool_eligible must be explicitly
    # persisted, not silently defaulted, when the caller supplies them.
    assert result.is_anonymized is False
    assert result.is_talent_pool_eligible is False
    db.add.assert_called_once()
    db.flush.assert_called_once()


def test_create_resume_embedding_defaults_flags_to_true_when_not_supplied():
    db, repo = _repo_with_mock_db()

    result, was_created = repo.create_resume_embedding(
        resume_id=uuid4(),
        candidate_id=uuid4(),
        embedding=[0.1] * 384,
        embedding_model_version_id=uuid4(),
        input_text_hash="abc123",
    )

    assert was_created is True
    assert result.is_anonymized is True
    assert result.is_talent_pool_eligible is True


def test_create_resume_embedding_falls_back_to_existing_row_on_integrity_error():
    """
    uq_resume_embeddings_resume_model_version (resume_id,
    embedding_model_version_id) is the DB-level backstop for two concurrent
    EMBED_RESUME runs racing on the same resume - the loser's flush raises
    IntegrityError, which must resolve to the winner's already-committed
    row instead of propagating and crashing the task.
    """
    db, repo = _repo_with_mock_db()
    db.flush.side_effect = IntegrityError("insert", {}, Exception("duplicate key"))
    existing_row = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = existing_row

    resume_id, model_version_id = uuid4(), uuid4()
    result, was_created = repo.create_resume_embedding(
        resume_id=resume_id,
        candidate_id=uuid4(),
        embedding=[0.1] * 384,
        embedding_model_version_id=model_version_id,
        input_text_hash="abc123",
    )

    assert was_created is False
    assert result is existing_row
