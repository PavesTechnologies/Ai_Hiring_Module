from unittest.mock import MagicMock
from uuid import uuid4

from app.repositories.resume_repository import ResumeRepository

"""GET /candidates (Global Candidates directory) - batched active-resume lookup."""


def _make_repo(resumes=None):
    db = MagicMock()
    db.execute.return_value.scalars.return_value.all.return_value = resumes or []
    return ResumeRepository(db), db


def test_returns_empty_dict_without_querying_when_no_ids():
    repo, db = _make_repo()

    result = repo.get_active_by_candidate_ids([])

    assert result == {}
    db.execute.assert_not_called()


def test_returns_dict_keyed_by_candidate_id():
    candidate_a, candidate_b = uuid4(), uuid4()
    resume_a = MagicMock(candidate_id=candidate_a)
    resume_b = MagicMock(candidate_id=candidate_b)
    repo, db = _make_repo([resume_a, resume_b])

    result = repo.get_active_by_candidate_ids([candidate_a, candidate_b])

    assert result == {candidate_a: resume_a, candidate_b: resume_b}


def test_candidate_with_no_active_resume_is_absent_from_result():
    candidate_with_resume = uuid4()
    candidate_without_resume = uuid4()
    resume = MagicMock(candidate_id=candidate_with_resume)
    repo, db = _make_repo([resume])

    result = repo.get_active_by_candidate_ids([candidate_with_resume, candidate_without_resume])

    assert candidate_with_resume in result
    assert candidate_without_resume not in result
