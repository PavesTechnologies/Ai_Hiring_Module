from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.exception_handler.exceptions import NotFoundError
from app.models.candidates import ParseStatus
from app.models.pipeline import PipelineStage
from app.services.talent_pool.talent_pool_service import TalentPoolService

"""
M13-E01 S01 T01/T02 - Access Unified Candidate Profile / Performance Summary.
"""


def _make_candidate(**overrides):
    defaults = dict(
        id=uuid4(),
        full_name_encrypted=b"encrypted-name",
        email_encrypted=b"encrypted-email",
        encryption_key_id=uuid4(),
        jurisdiction="IN",
        consent_given=True,
        consent_timestamp=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_resume(**overrides):
    defaults = dict(
        id=uuid4(),
        version_number=2,
        created_at=datetime.now(timezone.utc),
        parse_status=ParseStatus.PARSED,
        parsed_json={
            "total_experience_years": 5.5,
            "location": "Bengaluru",
            "work_experience": [{"title": "Senior Engineer", "is_current": True}],
        },
        parser_version="gemini-resume-extraction-v1",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_campaign_candidate(
    pipeline_stage=PipelineStage.SCREENING,
    composite_score=None,
    created_at=None,
):
    return SimpleNamespace(
        id=uuid4(),
        pipeline_stage=pipeline_stage,
        composite_score=composite_score,
        created_at=created_at or datetime.now(timezone.utc),
    )


def make_service(
    candidate_repo=None,
    resume_repo=None,
    campaign_candidate_repo=None,
    consent_repo=None,
    encryption_service=None,
):
    return TalentPoolService(
        candidate_repo=candidate_repo or MagicMock(),
        resume_repo=resume_repo or MagicMock(),
        campaign_repo=MagicMock(),
        campaign_candidate_repo=campaign_candidate_repo or MagicMock(),
        consent_repo=consent_repo or MagicMock(),
        encryption_service=encryption_service or MagicMock(),
        audit_service=MagicMock(),
        celery_task_log_service=MagicMock(),
        resume_selection_service=MagicMock(),
    )


def test_get_candidate_profile_raises_not_found_when_candidate_missing():
    candidate_repo = MagicMock()
    candidate_repo.get_by_id.return_value = None
    service = make_service(candidate_repo=candidate_repo)

    with pytest.raises(NotFoundError):
        service.get_candidate_profile(uuid4())


def test_get_candidate_profile_masks_email_and_decrypts_name():
    candidate = _make_candidate()
    resume = _make_resume()

    candidate_repo = MagicMock()
    candidate_repo.get_by_id.return_value = candidate

    resume_repo = MagicMock()
    resume_repo.get_active_by_candidate.return_value = resume
    resume_repo.get_embedding.return_value = SimpleNamespace(
        is_talent_pool_eligible=True, created_at=resume.created_at,
    )
    resume_repo.get_top_skills_by_candidate.return_value = ["Python", "SQL"]

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_all_by_candidate_across_campaigns.return_value = []

    consent_repo = MagicMock()
    consent_repo.get_latest_by_candidate.return_value = SimpleNamespace(consent_version="1.0")

    encryption_service = MagicMock()
    encryption_service.decrypt.side_effect = ["Jordan Lee", "jordan.lee@example.com"]

    service = make_service(
        candidate_repo=candidate_repo,
        resume_repo=resume_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        consent_repo=consent_repo,
        encryption_service=encryption_service,
    )

    profile = service.get_candidate_profile(candidate.id)

    assert profile.candidate.full_name == "Jordan Lee"
    assert profile.candidate.email == "j*********@example.com"
    assert profile.candidate.designation == "Senior Engineer"
    assert profile.candidate.experience == 5.5
    assert profile.candidate.location == "Bengaluru"
    assert profile.candidate.jurisdiction == "IN"
    assert profile.consent.consent_given is True
    assert profile.consent.consent_version == "1.0"
    assert profile.talent_pool.is_talent_pool_eligible is True
    assert profile.resume.active_resume_version == 2
    assert profile.resume.parse_status == ParseStatus.PARSED
    assert profile.campaign_summary.total_campaigns == 0
    assert profile.performance_summary.top_5_skills == ["Python", "SQL"]


def test_get_candidate_profile_derives_campaign_and_performance_summary():
    candidate = _make_candidate()
    resume = _make_resume()

    candidate_repo = MagicMock()
    candidate_repo.get_by_id.return_value = candidate

    resume_repo = MagicMock()
    resume_repo.get_active_by_candidate.return_value = resume
    resume_repo.get_embedding.return_value = None
    resume_repo.get_top_skills_by_candidate.return_value = []

    latest = _make_campaign_candidate(pipeline_stage=PipelineStage.SELECTED, composite_score=88.0)
    older = _make_campaign_candidate(pipeline_stage=PipelineStage.REJECTED, composite_score=40.0)

    campaign_candidate_repo = MagicMock()
    # Repository contract: most-recent-first.
    campaign_candidate_repo.get_all_by_candidate_across_campaigns.return_value = [
        (latest, "Campaign B", "Backend Engineer"),
        (older, "Campaign A", "Backend Engineer"),
    ]

    consent_repo = MagicMock()
    consent_repo.get_latest_by_candidate.return_value = None

    encryption_service = MagicMock()
    encryption_service.decrypt.side_effect = ["Jordan Lee", "jordan.lee@example.com"]

    service = make_service(
        candidate_repo=candidate_repo,
        resume_repo=resume_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        consent_repo=consent_repo,
        encryption_service=encryption_service,
    )

    profile = service.get_candidate_profile(candidate.id)

    assert profile.talent_pool.is_talent_pool_eligible is False
    assert profile.campaign_summary.total_campaigns == 2
    assert profile.campaign_summary.latest_campaign == "Campaign B"
    assert profile.campaign_summary.latest_pipeline_stage == PipelineStage.SELECTED

    performance = profile.performance_summary
    assert performance.best_composite_score == 88.0
    assert performance.campaign_name == "Campaign B"
    assert performance.jd_title == "Backend Engineer"
    assert performance.average_composite_score == 64.0
    assert performance.shortlisted_count == 0
    assert performance.selected_count == 1
    assert performance.total_campaigns == 2
