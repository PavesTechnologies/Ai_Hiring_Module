from contextlib import contextmanager
from unittest.mock import MagicMock
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from app.repositories.jd_repository import JDRepository


@contextmanager
def _reraising_savepoint():
    """
    Stands in for Session.begin_nested(): on a clean body it's a no-op; on
    an exception it does not swallow it, matching real SQLAlchemy behavior
    (a SAVEPOINT rollback happens internally, then the original exception
    still propagates to the caller's except block).
    """
    yield


def _repo_with_mock_db():
    db = MagicMock()
    db.begin_nested.side_effect = lambda: _reraising_savepoint()
    return db, JDRepository(db)


def test_create_jd_embedding_idempotent_returns_new_row_when_no_conflict():
    db, repo = _repo_with_mock_db()
    jd_id, model_version_id = uuid4(), uuid4()

    result, was_created = repo.create_jd_embedding_idempotent(
        jd_id=jd_id,
        embedding=[0.1] * 384,
        embedding_model_version_id=model_version_id,
        content_hash="abc123",
    )

    assert was_created is True
    assert result.jd_id == jd_id
    assert result.embedding_model_version_id == model_version_id
    assert result.input_text_hash == "abc123"
    db.add.assert_called_once()
    db.flush.assert_called_once()


def test_create_jd_embedding_idempotent_falls_back_to_existing_row_on_integrity_error():
    """
    jd_embeddings.jd_id is unique - two concurrent/duplicate attempts to
    create an embedding for the same jd_id must resolve to the winner's
    already-committed row instead of raising.
    """
    db, repo = _repo_with_mock_db()
    db.flush.side_effect = IntegrityError("insert", {}, Exception("duplicate key"))
    existing_row = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = existing_row

    result, was_created = repo.create_jd_embedding_idempotent(
        jd_id=uuid4(),
        embedding=[0.1] * 384,
        embedding_model_version_id=uuid4(),
        content_hash="abc123",
    )

    assert was_created is False
    assert result is existing_row


def test_get_embedding_by_content_hash_queries_by_hash_and_model_version():
    db = MagicMock()
    repo = JDRepository(db)
    expected = MagicMock()
    db.execute.return_value.scalars.return_value.first.return_value = expected

    result = repo.get_embedding_by_content_hash("somehash", uuid4())

    assert result is expected
    db.execute.assert_called_once()


def test_get_embedding_by_content_hash_returns_none_when_no_match():
    db = MagicMock()
    repo = JDRepository(db)
    db.execute.return_value.scalars.return_value.first.return_value = None

    result = repo.get_embedding_by_content_hash("somehash", uuid4())

    assert result is None
