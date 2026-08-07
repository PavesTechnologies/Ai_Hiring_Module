from unittest.mock import MagicMock
from uuid import uuid4

from app.repositories.resume_repository import ResumeRepository

"""
M13-E01 S01 T02 - ResumeRepository.get_top_skills_by_candidate.
"""


def test_get_top_skills_by_candidate_returns_names_ordered_by_occurrence():
    db = MagicMock()
    db.execute.return_value.all.return_value = [("Python", 5), ("SQL", 3)]
    repo = ResumeRepository(db)

    result = repo.get_top_skills_by_candidate(uuid4(), limit=5)

    assert result == ["Python", "SQL"]
    db.execute.assert_called_once()


def test_get_top_skills_by_candidate_returns_empty_list_when_no_matches():
    db = MagicMock()
    db.execute.return_value.all.return_value = []
    repo = ResumeRepository(db)

    result = repo.get_top_skills_by_candidate(uuid4())

    assert result == []
