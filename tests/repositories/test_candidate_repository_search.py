from unittest.mock import MagicMock

from app.repositories.candidate_repository import CandidateRepository

"""GET /candidates (Global Candidates directory) - CandidateRepository.search/count_search."""


def _make_repo(candidates=None, count=0):
    db = MagicMock()
    db.execute.return_value.scalars.return_value.all.return_value = candidates or []
    db.execute.return_value.scalar_one.return_value = count
    return CandidateRepository(db), db


def test_search_with_no_filters_returns_a_page_of_candidates():
    candidates = [MagicMock(), MagicMock()]
    repo, db = _make_repo(candidates)

    result = repo.search(page=1, size=20)

    assert result == candidates
    db.execute.assert_called_once()


def test_search_applies_email_hash_filter():
    repo, db = _make_repo([])

    repo.search(email_hash="abc123")

    compiled = str(db.execute.call_args.args[0])
    assert "email_hash" in compiled


def test_search_applies_jurisdiction_filter():
    repo, db = _make_repo([])

    repo.search(jurisdiction="EU")

    compiled = str(db.execute.call_args.args[0])
    assert "jurisdiction" in compiled


def test_search_paginates_via_offset_and_limit():
    repo, db = _make_repo([])

    repo.search(page=3, size=10)

    compiled = str(db.execute.call_args.args[0])
    assert "OFFSET" in compiled.upper()
    assert "LIMIT" in compiled.upper()


def test_count_search_returns_scalar_count():
    repo, db = _make_repo(count=42)

    result = repo.count_search()

    assert result == 42
