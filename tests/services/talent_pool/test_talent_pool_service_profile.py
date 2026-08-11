from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.exception_handler.exceptions import NotFoundError
from app.models.candidates import ParseStatus
from app.models.pipeline import PipelineStage
from app.services.campaign.resume_selection_service import ResumeSelectionService
from app.services.talent_pool.talent_pool_service import TalentPoolService

"""
M13-E01 S01 T01/T02 - Access Unified Candidate Profile / Performance Summary.

M13-E01 S02 (Fix Candidate Profile Talent Pool Eligibility Display) -
profile.talent_pool.is_talent_pool_eligible now reflects EFFECTIVE
eligibility (ResumeSelectionService._is_eligible: PARSED + embedding exists
+ is_talent_pool_eligible + freshness), not the raw stored
resume_embeddings.is_talent_pool_eligible flag. The freshness-specific
tests below construct a REAL ResumeSelectionService (its _is_fresh/
_is_eligible are pure, side-effect-free reads) so they exercise genuine age
arithmetic against RESUME_FRESHNESS_MAX_AGE_DAYS, rather than re-asserting
a mock's own canned return value.
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
    resume_selection_service=None,
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
        resume_selection_service=resume_selection_service or MagicMock(),
    )


def _real_resume_selection_service(resume_repo, max_age_days=None):
    """
    A genuine ResumeSelectionService (not mocked) backing
    _is_eligible/_is_fresh, sharing the SAME resume_repo mock the
    TalentPoolService under test uses for display - so "what's shown" and
    "what determines eligibility" are checked against consistent data,
    exactly like the real dependency graph.
    """
    config_repo = MagicMock()
    config_repo.get_configs_by_keys.return_value = (
        {"RESUME_FRESHNESS_MAX_AGE_DAYS": str(max_age_days)} if max_age_days is not None else {}
    )
    return ResumeSelectionService(
        resume_repo=resume_repo, jd_repo=MagicMock(), config_repo=config_repo,
        candidate_scoring_service=MagicMock(),
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

    resume_selection_service = MagicMock()
    resume_selection_service._is_eligible.return_value = True

    service = make_service(
        candidate_repo=candidate_repo,
        resume_repo=resume_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        consent_repo=consent_repo,
        encryption_service=encryption_service,
        resume_selection_service=resume_selection_service,
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
    assert profile.resume.resume_id == resume.id
    assert profile.resume.active_resume_version == 2
    assert profile.resume.parse_status == ParseStatus.PARSED
    # Default _make_resume() parsed_json has no "summary" key.
    assert profile.resume.summary is None
    assert profile.campaign_summary.total_campaigns == 0
    assert profile.performance_summary.top_5_skills == ["Python", "SQL"]
    resume_selection_service._is_eligible.assert_called_once_with(resume)


def test_get_candidate_profile_includes_summary_from_active_resume_parsed_json():
    """M13-E01 S02 T0x - profile Summary tab reads the same parsed_json.summary field the search card does."""
    candidate = _make_candidate()
    resume = _make_resume(parsed_json={
        "total_experience_years": 5.5,
        "location": "Bengaluru",
        "work_experience": [{"title": "Senior Engineer", "is_current": True}],
        "summary": "Senior backend engineer with 5+ years building distributed systems.",
    })

    candidate_repo = MagicMock()
    candidate_repo.get_by_id.return_value = candidate

    resume_repo = MagicMock()
    resume_repo.get_active_by_candidate.return_value = resume
    resume_repo.get_embedding.return_value = None
    resume_repo.get_top_skills_by_candidate.return_value = []

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_all_by_candidate_across_campaigns.return_value = []

    consent_repo = MagicMock()
    consent_repo.get_latest_by_candidate.return_value = None

    encryption_service = MagicMock()
    encryption_service.decrypt.side_effect = ["Jordan Lee", "jordan.lee@example.com"]

    resume_selection_service = MagicMock()
    resume_selection_service._is_eligible.return_value = False

    service = make_service(
        candidate_repo=candidate_repo,
        resume_repo=resume_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        consent_repo=consent_repo,
        encryption_service=encryption_service,
        resume_selection_service=resume_selection_service,
    )

    profile = service.get_candidate_profile(candidate.id)

    assert profile.resume.summary == "Senior backend engineer with 5+ years building distributed systems."
    assert profile.resume.resume_id == resume.id


def test_get_candidate_profile_has_no_resume_id_or_summary_when_no_active_resume():
    candidate = _make_candidate()

    candidate_repo = MagicMock()
    candidate_repo.get_by_id.return_value = candidate

    resume_repo = MagicMock()
    resume_repo.get_active_by_candidate.return_value = None
    resume_repo.get_embedding.return_value = None
    resume_repo.get_top_skills_by_candidate.return_value = []

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_all_by_candidate_across_campaigns.return_value = []

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

    assert profile.resume.resume_id is None
    assert profile.resume.summary is None
    assert profile.resume.active_resume_version is None


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

    # This test is about campaign/performance summary derivation, not
    # eligibility specifics - a real ResumeSelectionService against a
    # missing embedding (resume_repo.get_embedding returns None above)
    # correctly computes ineligible without needing a canned mock value.
    resume_selection_service = _real_resume_selection_service(resume_repo)

    service = make_service(
        candidate_repo=candidate_repo,
        resume_repo=resume_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        consent_repo=consent_repo,
        encryption_service=encryption_service,
        resume_selection_service=resume_selection_service,
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


# ----------------------------------------------------------------------
# M13-E01 S02 - effective Talent Pool eligibility display (freshness-aware)
# ----------------------------------------------------------------------

def _profile_eligibility_for(resume, embedding, max_age_days=None):
    """
    Builds a full TalentPoolService around one resume/embedding pair using
    a REAL ResumeSelectionService, and returns just the eligibility flag
    the profile displays - the thing every test in this section checks.
    """
    candidate = _make_candidate()

    resume_repo = MagicMock()
    resume_repo.get_active_by_candidate.return_value = resume
    resume_repo.get_embedding.return_value = embedding
    resume_repo.get_top_skills_by_candidate.return_value = []

    candidate_repo = MagicMock()
    candidate_repo.get_by_id.return_value = candidate

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_all_by_candidate_across_campaigns.return_value = []

    consent_repo = MagicMock()
    consent_repo.get_latest_by_candidate.return_value = None

    encryption_service = MagicMock()
    encryption_service.decrypt.side_effect = ["Jordan Lee", "jordan.lee@example.com"]

    resume_selection_service = _real_resume_selection_service(resume_repo, max_age_days=max_age_days)

    service = make_service(
        candidate_repo=candidate_repo,
        resume_repo=resume_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        consent_repo=consent_repo,
        encryption_service=encryption_service,
        resume_selection_service=resume_selection_service,
    )

    return service.get_candidate_profile(candidate.id).talent_pool.is_talent_pool_eligible


def test_profile_shows_eligible_for_fresh_resume_with_eligible_embedding():
    resume = _make_resume(
        parse_status=ParseStatus.PARSED, created_at=datetime.now(timezone.utc) - timedelta(days=100),
    )
    embedding = SimpleNamespace(is_talent_pool_eligible=True, created_at=resume.created_at)

    assert _profile_eligibility_for(resume, embedding) is True


def test_profile_shows_not_eligible_for_stale_resume_with_eligible_embedding():
    """The exact bug this fix addresses: 200 days old + is_talent_pool_eligible=True must NOT show as eligible."""
    resume = _make_resume(
        parse_status=ParseStatus.PARSED, created_at=datetime.now(timezone.utc) - timedelta(days=200),
    )
    embedding = SimpleNamespace(is_talent_pool_eligible=True, created_at=resume.created_at)

    assert _profile_eligibility_for(resume, embedding) is False


def test_profile_shows_not_eligible_for_stale_resume_with_ineligible_embedding():
    resume = _make_resume(
        parse_status=ParseStatus.PARSED, created_at=datetime.now(timezone.utc) - timedelta(days=200),
    )
    embedding = SimpleNamespace(is_talent_pool_eligible=False, created_at=resume.created_at)

    assert _profile_eligibility_for(resume, embedding) is False


def test_profile_shows_not_eligible_for_fresh_resume_with_ineligible_embedding():
    resume = _make_resume(
        parse_status=ParseStatus.PARSED, created_at=datetime.now(timezone.utc) - timedelta(days=5),
    )
    embedding = SimpleNamespace(is_talent_pool_eligible=False, created_at=resume.created_at)

    assert _profile_eligibility_for(resume, embedding) is False


def test_profile_shows_not_eligible_when_embedding_missing():
    resume = _make_resume(
        parse_status=ParseStatus.PARSED, created_at=datetime.now(timezone.utc) - timedelta(days=5),
    )

    assert _profile_eligibility_for(resume, embedding=None) is False


def test_profile_respects_configured_freshness_threshold():
    """Not hardcoded 180 - a shorter configured threshold excludes a resume that would pass the default."""
    resume = _make_resume(
        parse_status=ParseStatus.PARSED, created_at=datetime.now(timezone.utc) - timedelta(days=45),
    )
    embedding = SimpleNamespace(is_talent_pool_eligible=True, created_at=resume.created_at)

    assert _profile_eligibility_for(resume, embedding, max_age_days=30) is False
