from unittest.mock import MagicMock
from uuid import uuid4

from app.repositories.resume_repository import ResumeRepository

"""M13-E01 S02 (Talent Pool Search) - ResumeRepository.get_by_skill_match / get_all_parsed."""


def _make_repo(rows=None):
    db = MagicMock()
    db.execute.return_value.scalars.return_value.all.return_value = rows or []
    return ResumeRepository(db), db


def test_get_by_skill_match_queries_with_canonical_id_and_raw_text_pattern():
    canonical_id = uuid4()
    repo, db = _make_repo()

    repo.get_by_skill_match(canonical_skill_id=canonical_id, raw_text_pattern="%java%")

    db.execute.assert_called_once()


def test_get_by_skill_match_works_without_a_resolved_canonical_id():
    """Falls back to raw-text-only matching when the skill isn't in the ontology yet."""
    repo, db = _make_repo()

    repo.get_by_skill_match(canonical_skill_id=None, raw_text_pattern="%java%")

    db.execute.assert_called_once()


def test_get_by_skill_match_returns_matched_resumes():
    resume = MagicMock()
    repo, db = _make_repo([resume])

    result = repo.get_by_skill_match(canonical_skill_id=None, raw_text_pattern="%java%")

    assert result == [resume]


def test_get_all_parsed_returns_resumes():
    resume = MagicMock()
    repo, db = _make_repo([resume])

    result = repo.get_all_parsed()

    assert result == [resume]
    db.execute.assert_called_once()
