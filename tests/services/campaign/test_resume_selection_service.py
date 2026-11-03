from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.exception_handler.exceptions import UnprocessableError
from app.models.candidates import ParseStatus
from app.services.campaign.resume_selection_service import ResumeSelectionService, SelectionMethod

"""
M13-E01 S01 T03 refinement - ResumeSelectionService: branches on eligible
resume count (0/1/>1), never raw resume count.
"""


def _resume(parse_status=ParseStatus.PARSED, parsed_json=None, created_at=None):
    return SimpleNamespace(
        id=uuid4(),
        version_number=1,
        parse_status=parse_status,
        parsed_json=parsed_json if parsed_json is not None else {},
        # Defaults to "just created" so every pre-existing test (none of
        # which care about freshness) stays comfortably inside any
        # reasonable RESUME_FRESHNESS_MAX_AGE_DAYS without having to know
        # about the freshness rule at all.
        created_at=created_at if created_at is not None else datetime.now(timezone.utc),
    )


def _embedding(is_talent_pool_eligible=True):
    return SimpleNamespace(is_talent_pool_eligible=is_talent_pool_eligible)


def _campaign(weight_deterministic=30.0, weight_semantic=40.0, deterministic_threshold=70.0, semantic_threshold=0.65):
    return SimpleNamespace(
        jd_id=uuid4(),
        weight_deterministic=weight_deterministic,
        weight_semantic=weight_semantic,
        deterministic_threshold=deterministic_threshold,
        semantic_threshold=semantic_threshold,
        required_skill_coverage_threshold=0.0,
        max_missing_core_skills=3,
    )


def _job_description(jd_id=None, min_experience_years=None, education_criteria=None, extracted_json=None):
    return SimpleNamespace(
        id=jd_id or uuid4(),
        min_experience_years=min_experience_years,
        education_criteria=education_criteria,
        extracted_json=extracted_json,
    )


def make_service(
    resume_repo=None,
    jd_repo=None,
    config_repo=None,
):
    resume_repo = resume_repo or MagicMock()
    config_repo = config_repo or MagicMock()
    config_repo.get_configs_by_keys.return_value = {}
    return ResumeSelectionService(
        resume_repo=resume_repo,
        jd_repo=jd_repo or MagicMock(),
        config_repo=config_repo,
    )


def _with_embedding_map(resume_repo, embedding_by_resume_id: dict):
    resume_repo.get_embedding.side_effect = lambda resume_id: embedding_by_resume_id.get(resume_id)


# ----------------------------------------------------------------------
# Eligible-count branching
# ----------------------------------------------------------------------

def test_zero_eligible_raises_unprocessable():
    unparsed = _resume(parse_status=ParseStatus.PENDING)
    resume_repo = MagicMock()
    resume_repo.get_all_versions_by_candidate.return_value = [unparsed]
    _with_embedding_map(resume_repo, {})
    service = make_service(resume_repo=resume_repo)

    with pytest.raises(UnprocessableError):
        service.select_resume_for_campaign(uuid4(), _campaign())


def test_single_eligible_returns_direct_without_scoring():
    resume = _resume()
    resume_repo = MagicMock()
    resume_repo.get_all_versions_by_candidate.return_value = [resume]
    _with_embedding_map(resume_repo, {resume.id: _embedding()})
    jd_repo = MagicMock()

    service = make_service(resume_repo=resume_repo, jd_repo=jd_repo)

    result = service.select_resume_for_campaign(uuid4(), _campaign())

    assert result.selection_method == SelectionMethod.DIRECT
    assert result.selected_resume is resume
    assert len(result.evaluated_resumes) == 1
    assert result.evaluated_resumes[0].is_selected is True
    assert result.evaluated_resumes[0].deterministic_score is None
    assert result.evaluated_resumes[0].semantic_score is None
    # Nothing scoring-related should have run at all - nothing to compare.
    jd_repo.get_by_id.assert_not_called()


def test_not_talent_pool_eligible_resume_excluded():
    ineligible = _resume()
    eligible = _resume()
    resume_repo = MagicMock()
    resume_repo.get_all_versions_by_candidate.return_value = [ineligible, eligible]
    _with_embedding_map(resume_repo, {
        ineligible.id: _embedding(is_talent_pool_eligible=False),
        eligible.id: _embedding(is_talent_pool_eligible=True),
    })
    service = make_service(resume_repo=resume_repo)

    result = service.select_resume_for_campaign(uuid4(), _campaign())

    assert result.selection_method == SelectionMethod.DIRECT
    assert result.selected_resume is eligible


def test_resume_with_no_embedding_row_excluded():
    no_embedding = _resume()
    eligible = _resume()
    resume_repo = MagicMock()
    resume_repo.get_all_versions_by_candidate.return_value = [no_embedding, eligible]
    _with_embedding_map(resume_repo, {eligible.id: _embedding()})
    service = make_service(resume_repo=resume_repo)

    result = service.select_resume_for_campaign(uuid4(), _campaign())

    assert result.selection_method == SelectionMethod.DIRECT
    assert result.selected_resume is eligible


# ----------------------------------------------------------------------
# Talent Pool Eligibility - resume freshness (6-month rule)
# ----------------------------------------------------------------------

def test_resume_older_than_default_max_age_excluded():
    stale = _resume(created_at=datetime.now(timezone.utc) - timedelta(days=181))
    fresh = _resume(created_at=datetime.now(timezone.utc) - timedelta(days=1))
    resume_repo = MagicMock()
    resume_repo.get_all_versions_by_candidate.return_value = [stale, fresh]
    _with_embedding_map(resume_repo, {stale.id: _embedding(), fresh.id: _embedding()})
    service = make_service(resume_repo=resume_repo)

    result = service.select_resume_for_campaign(uuid4(), _campaign())

    assert result.selection_method == SelectionMethod.DIRECT
    assert result.selected_resume is fresh


def test_resume_just_under_max_age_boundary_is_still_eligible():
    # A hair under 180 days, not exactly 180 - testing the literal instant
    # of the boundary against real wall-clock time would be flaky (the
    # elapsed time between fixture setup and the service call under test
    # would tip an exact 180-day-old resume over the edge unpredictably).
    boundary = _resume(created_at=datetime.now(timezone.utc) - timedelta(days=180) + timedelta(minutes=5))
    resume_repo = MagicMock()
    resume_repo.get_all_versions_by_candidate.return_value = [boundary]
    _with_embedding_map(resume_repo, {boundary.id: _embedding()})
    service = make_service(resume_repo=resume_repo)

    result = service.select_resume_for_campaign(uuid4(), _campaign())

    assert result.selected_resume is boundary


def test_all_resumes_stale_raises_unprocessable_like_any_other_zero_eligible_case():
    stale = _resume(created_at=datetime.now(timezone.utc) - timedelta(days=200))
    resume_repo = MagicMock()
    resume_repo.get_all_versions_by_candidate.return_value = [stale]
    _with_embedding_map(resume_repo, {stale.id: _embedding()})
    service = make_service(resume_repo=resume_repo)

    with pytest.raises(UnprocessableError):
        service.select_resume_for_campaign(uuid4(), _campaign())


def test_freshness_respects_configured_max_age_days():
    resume = _resume(created_at=datetime.now(timezone.utc) - timedelta(days=45))
    resume_repo = MagicMock()
    resume_repo.get_all_versions_by_candidate.return_value = [resume]
    _with_embedding_map(resume_repo, {resume.id: _embedding()})
    service = make_service(resume_repo=resume_repo)
    # make_service always defaults config_repo.get_configs_by_keys to {} -
    # override on the constructed instance rather than fighting that default.
    service.config_repo.get_configs_by_keys.return_value = {"RESUME_FRESHNESS_MAX_AGE_DAYS": "30"}

    with pytest.raises(UnprocessableError):
        service.select_resume_for_campaign(uuid4(), _campaign())


def test_candidate_reactivated_by_uploading_a_new_resume_after_going_stale():
    """
    Candidate Reactivation Through a New Resume: a candidate whose only
    resume has gone stale has zero eligible resumes (would 422). Once they
    upload a new resume - a brand-new Resume row with its own fresh
    created_at and its own fresh embedding - get_all_versions_by_candidate
    now returns both, and the new one is found eligible and selected. No
    candidate-level flag needs resetting; eligibility is recomputed fresh
    per resume version on every call.
    """
    candidate_id = uuid4()

    stale_only = _resume(created_at=datetime.now(timezone.utc) - timedelta(days=181))
    resume_repo = MagicMock()
    resume_repo.get_all_versions_by_candidate.return_value = [stale_only]
    _with_embedding_map(resume_repo, {stale_only.id: _embedding()})
    service = make_service(resume_repo=resume_repo)

    with pytest.raises(UnprocessableError):
        service.select_resume_for_campaign(candidate_id, _campaign())

    # Candidate uploads a new resume - a new version is created for them.
    reactivating_resume = _resume(created_at=datetime.now(timezone.utc))
    resume_repo.get_all_versions_by_candidate.return_value = [stale_only, reactivating_resume]
    _with_embedding_map(resume_repo, {
        stale_only.id: _embedding(), reactivating_resume.id: _embedding(),
    })

    result = service.select_resume_for_campaign(candidate_id, _campaign())

    assert result.selection_method == SelectionMethod.DIRECT
    assert result.selected_resume is reactivating_resume


def test_freshness_is_independent_of_is_talent_pool_eligible():
    """Age-stale but otherwise talent-pool-eligible must still be excluded - two distinct gates."""
    stale_but_eligible_flag = _resume(created_at=datetime.now(timezone.utc) - timedelta(days=365))
    resume_repo = MagicMock()
    resume_repo.get_all_versions_by_candidate.return_value = [stale_but_eligible_flag]
    _with_embedding_map(resume_repo, {stale_but_eligible_flag.id: _embedding(is_talent_pool_eligible=True)})
    service = make_service(resume_repo=resume_repo)

    with pytest.raises(UnprocessableError):
        service.select_resume_for_campaign(uuid4(), _campaign())


# ----------------------------------------------------------------------
# Multi-resume comparison
# ----------------------------------------------------------------------

def test_multiple_eligible_select_the_latest_parsed_version_without_scoring():
    """Retire v1, phase C (the user's choice): the latest version wins; nothing is scored to pick it."""
    older, latest, middle = _resume(), _resume(), _resume()
    older.version_number, latest.version_number, middle.version_number = 1, 3, 2
    resume_repo = MagicMock()
    resume_repo.get_all_versions_by_candidate.return_value = [older, latest, middle]
    _with_embedding_map(resume_repo, {r.id: _embedding() for r in (older, latest, middle)})
    service = make_service(resume_repo=resume_repo)
    result = service.select_resume_for_campaign(uuid4(), _campaign())

    assert result.selection_method == SelectionMethod.LATEST
    assert result.selected_resume is latest
    assert [e.resume for e in result.evaluated_resumes if e.is_selected] == [latest]
    assert len(result.evaluated_resumes) == 3 and all(e.selection_score is None for e in result.evaluated_resumes)
    resume_repo.compute_semantic_similarity.assert_not_called()


# ----------------------------------------------------------------------
# Weight renormalization
# ----------------------------------------------------------------------
