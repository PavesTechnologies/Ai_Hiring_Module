from unittest.mock import MagicMock
from uuid import uuid4

from app.repositories.resume_repository import ResumeRepository


def _repo_with_mock_db():
    db = MagicMock()
    return db, ResumeRepository(db)


# ----------------------------------------------------------------------
# Embedding Storage Dashboard
# ----------------------------------------------------------------------

def test_count_embeddings_returns_scalar_count():
    db, repo = _repo_with_mock_db()
    db.query.return_value.scalar.return_value = 42

    assert repo.count_embeddings() == 42


def test_count_embeddings_returns_zero_when_scalar_is_none():
    db, repo = _repo_with_mock_db()
    db.query.return_value.scalar.return_value = None

    assert repo.count_embeddings() == 0


def test_get_ivfflat_index_health_reports_exists_true_when_index_found():
    db, repo = _repo_with_mock_db()
    db.execute.return_value.first.return_value = MagicMock(
        index_name="idx_resume_embeddings_embedding", size_bytes=1024, scan_count=5,
    )

    health = repo.get_ivfflat_index_health()

    assert health["exists"] is True
    assert health["index_name"] == "idx_resume_embeddings_embedding"
    assert health["size_bytes"] == 1024
    assert health["scan_count"] == 5


def test_get_ivfflat_index_health_reports_exists_false_when_missing():
    db, repo = _repo_with_mock_db()
    db.execute.return_value.first.return_value = None

    health = repo.get_ivfflat_index_health()

    assert health["exists"] is False
    assert health["size_bytes"] is None


# ----------------------------------------------------------------------
# Talent Pool Eligibility reconciliation query helpers
# ----------------------------------------------------------------------

def test_get_distinct_candidate_ids_with_embeddings():
    db, repo = _repo_with_mock_db()
    ids = [uuid4(), uuid4()]
    db.execute.return_value.scalars.return_value.all.return_value = ids

    result = repo.get_distinct_candidate_ids_with_embeddings()

    assert result == ids


def test_get_embeddings_by_candidate():
    db, repo = _repo_with_mock_db()
    rows = [MagicMock(), MagicMock()]
    db.execute.return_value.scalars.return_value.all.return_value = rows

    result = repo.get_embeddings_by_candidate(uuid4())

    assert result == rows


def test_set_talent_pool_eligibility_for_candidate_returns_rowcount():
    db, repo = _repo_with_mock_db()
    db.execute.return_value.rowcount = 3

    result = repo.set_talent_pool_eligibility_for_candidate(uuid4(), True)

    assert result == 3
    db.flush.assert_called_once()


# ----------------------------------------------------------------------
# Candidate Erasure (requested phase)
# ----------------------------------------------------------------------

def test_zero_out_embeddings_for_candidate_returns_rowcount():
    db, repo = _repo_with_mock_db()
    db.execute.return_value.rowcount = 2

    result = repo.zero_out_embeddings_for_candidate(uuid4())

    assert result == 2
    db.flush.assert_called_once()
    db.execute.assert_called_once()
