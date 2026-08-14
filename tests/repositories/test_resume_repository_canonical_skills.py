from unittest.mock import MagicMock
from uuid import uuid4

from app.repositories.resume_repository import ResumeRepository

"""
M13-E01 S02 T0x - ResumeRepository.get_canonical_skills_by_resume_ids.
Batched counterpart to get_top_skills_by_candidate: per-resume, unranked,
every canonical skill (not top-N), keyed by resume_id for a whole page of
resumes in one query.
"""


def test_returns_skills_grouped_by_resume_id():
    resume_a, resume_b = uuid4(), uuid4()
    db = MagicMock()
    db.execute.return_value.all.return_value = [
        (resume_a, "Java"),
        (resume_a, "Spring Boot"),
        (resume_b, "Docker"),
    ]
    repo = ResumeRepository(db)

    result = repo.get_canonical_skills_by_resume_ids([resume_a, resume_b])

    assert result == {resume_a: ["Java", "Spring Boot"], resume_b: ["Docker"]}
    db.execute.assert_called_once()


def test_candidate_with_no_skills_is_absent_from_result():
    resume_id = uuid4()
    db = MagicMock()
    db.execute.return_value.all.return_value = []
    repo = ResumeRepository(db)

    result = repo.get_canonical_skills_by_resume_ids([resume_id])

    assert result == {}


def test_empty_resume_id_list_short_circuits_without_a_query():
    db = MagicMock()
    repo = ResumeRepository(db)

    result = repo.get_canonical_skills_by_resume_ids([])

    assert result == {}
    db.execute.assert_not_called()
