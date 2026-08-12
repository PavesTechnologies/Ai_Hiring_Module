from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.exception_handler.exceptions import BadRequestError, NotFoundError
from app.models.candidates import ParseStatus
from app.services.resume.resume_monitoring_service import ResumeMonitoringService

"""S02-T02 - Compare Parsed Data Between Two Resume Versions."""


def _make_resume(candidate_id, version_number, parsed_json, resume_id=None):
    return SimpleNamespace(
        id=resume_id or uuid4(),
        candidate_id=candidate_id,
        version_number=version_number,
        parse_status=ParseStatus.PARSED,
        parsed_json=parsed_json,
        created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )


def _make_service(resume_repository):
    return ResumeMonitoringService(
        resume_repository=resume_repository,
        candidate_repository=MagicMock(),
        encryption_service=MagicMock(),
        task_log_repository=MagicMock(),
        stage_repository=MagicMock(),
        stage_failure_log_repository=MagicMock(),
        dead_letter_queue_repository=MagicMock(),
        storage_service=MagicMock(),
        campaign_candidate_repository=MagicMock(),
        user_repository=MagicMock(),
        config_repository=MagicMock(),
    )


def test_compare_resume_versions_rejects_the_same_resume_id_twice():
    resume_repository = MagicMock()
    service = _make_service(resume_repository)
    resume_id = uuid4()

    with pytest.raises(BadRequestError):
        service.compare_resume_versions(resume_id, resume_id)

    resume_repository.get_by_id.assert_not_called()


def test_compare_resume_versions_raises_not_found_for_unknown_resume():
    resume_repository = MagicMock()
    resume_repository.get_by_id.return_value = None
    service = _make_service(resume_repository)

    with pytest.raises(NotFoundError):
        service.compare_resume_versions(uuid4(), uuid4())


def test_compare_resume_versions_rejects_resumes_from_different_candidates():
    resume_1 = _make_resume(uuid4(), 1, {})
    resume_2 = _make_resume(uuid4(), 2, {})
    resume_repository = MagicMock()
    resume_repository.get_by_id.side_effect = [resume_1, resume_2]
    service = _make_service(resume_repository)

    with pytest.raises(BadRequestError):
        service.compare_resume_versions(resume_1.id, resume_2.id)


def test_compare_resume_versions_returns_full_diff_and_summary():
    candidate_id = uuid4()
    parsed_1 = {
        "skills": ["Python", "SQL"],
        "work_experience": [{"title": "Engineer", "company": "Acme", "start_date": "2019", "end_date": "2021", "is_current": False}],
        "education": [{"degree": "BSc", "institution": "MIT", "field": "CS", "graduation_year": 2018}],
        "total_experience_years": 3.0,
    }
    parsed_2 = {
        "skills": ["Python", "AWS"],
        "work_experience": [
            {"title": "Engineer", "company": "Acme", "start_date": "2019", "end_date": "2021", "is_current": False},
            {"title": "Senior Engineer", "company": "Acme", "start_date": "2021", "end_date": None, "is_current": True},
        ],
        "education": [
            {"degree": "BSc", "institution": "MIT", "field": "CS", "graduation_year": 2018},
            {"degree": "MSc", "institution": "Stanford", "field": "AI", "graduation_year": 2020},
        ],
        "total_experience_years": 5.0,
    }
    resume_1 = _make_resume(candidate_id, 1, parsed_1)
    resume_2 = _make_resume(candidate_id, 2, parsed_2)
    resume_repository = MagicMock()
    resume_repository.get_by_id.side_effect = [resume_1, resume_2]
    service = _make_service(resume_repository)

    result = service.compare_resume_versions(resume_1.id, resume_2.id)

    assert result.candidate_id == candidate_id
    assert result.version_1.resume_id == resume_1.id
    assert result.version_2.resume_id == resume_2.id
    assert result.version_1.parsed_json == parsed_1
    assert result.version_2.parsed_json == parsed_2

    assert result.skills.added == ["AWS"]
    assert result.skills.removed == ["SQL"]
    assert result.skills.unchanged == ["Python"]

    assert len(result.experience.added) == 1
    assert result.experience.added[0].title == "Senior Engineer"
    assert result.experience.removed == []

    assert len(result.education.added) == 1
    assert result.education.added[0].degree == "MSc"
    assert result.education.removed == []

    assert result.experience_years.version_1 == 3.0
    assert result.experience_years.version_2 == 5.0
    assert result.experience_years.difference == 2.0

    assert result.summary.skills_added == 1
    assert result.summary.skills_removed == 1
    assert result.summary.skills_unchanged == 1
    assert result.summary.experience_years_change == 2.0


def test_compare_resume_versions_handles_missing_parsed_json_gracefully():
    """A resume that hasn't been parsed yet (parsed_json=None) must not crash the comparison."""
    candidate_id = uuid4()
    resume_1 = _make_resume(candidate_id, 1, None)
    resume_2 = _make_resume(candidate_id, 2, {"skills": ["Python"]})
    resume_repository = MagicMock()
    resume_repository.get_by_id.side_effect = [resume_1, resume_2]
    service = _make_service(resume_repository)

    result = service.compare_resume_versions(resume_1.id, resume_2.id)

    assert result.skills.added == ["Python"]
    assert result.skills.removed == []
    assert result.version_1.parsed_json == {}


def test_compare_resume_versions_never_persists_anything():
    """Read-only per spec — the resume_repository's db session must never be flushed/committed."""
    candidate_id = uuid4()
    resume_1 = _make_resume(candidate_id, 1, {"skills": ["Python"]})
    resume_2 = _make_resume(candidate_id, 2, {"skills": ["Python", "AWS"]})
    resume_repository = MagicMock()
    resume_repository.get_by_id.side_effect = [resume_1, resume_2]
    service = _make_service(resume_repository)

    service.compare_resume_versions(resume_1.id, resume_2.id)

    resume_repository.create.assert_not_called()
    resume_repository.delete.assert_not_called()
