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


def _resume(parse_status=ParseStatus.PARSED, parsed_json=None):
    return SimpleNamespace(
        id=uuid4(),
        version_number=1,
        parse_status=parse_status,
        parsed_json=parsed_json if parsed_json is not None else {},
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
    candidate_scoring_service=None,
):
    resume_repo = resume_repo or MagicMock()
    config_repo = config_repo or MagicMock()
    config_repo.get_configs_by_keys.return_value = {}
    return ResumeSelectionService(
        resume_repo=resume_repo,
        jd_repo=jd_repo or MagicMock(),
        config_repo=config_repo,
        candidate_scoring_service=candidate_scoring_service or MagicMock(),
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
    candidate_scoring_service = MagicMock()

    service = make_service(resume_repo=resume_repo, jd_repo=jd_repo, candidate_scoring_service=candidate_scoring_service)

    result = service.select_resume_for_campaign(uuid4(), _campaign())

    assert result.selection_method == SelectionMethod.DIRECT
    assert result.selected_resume is resume
    assert len(result.evaluated_resumes) == 1
    assert result.evaluated_resumes[0].is_selected is True
    assert result.evaluated_resumes[0].deterministic_score is None
    assert result.evaluated_resumes[0].semantic_score is None
    # Nothing scoring-related should have run at all - nothing to compare.
    jd_repo.get_by_id.assert_not_called()
    candidate_scoring_service.build_mandatory_skill_breakdown.assert_not_called()


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
# Multi-resume comparison
# ----------------------------------------------------------------------

def test_multiple_eligible_selects_highest_selection_score():
    weak_resume = _resume()
    strong_resume = _resume()
    resume_repo = MagicMock()
    resume_repo.get_all_versions_by_candidate.return_value = [weak_resume, strong_resume]
    _with_embedding_map(resume_repo, {weak_resume.id: _embedding(), strong_resume.id: _embedding()})
    resume_repo.compute_semantic_similarity.side_effect = lambda resume_id, jd_id: (
        0.50 if resume_id == weak_resume.id else 0.90
    )

    jd_repo = MagicMock()
    job_description = _job_description()
    jd_repo.get_by_id.return_value = job_description
    jd_repo.get_embedding_by_jd_id.return_value = SimpleNamespace(id=uuid4())  # JD has an embedding

    candidate_scoring_service = MagicMock()
    candidate_scoring_service.build_mandatory_skill_breakdown.return_value = {
        "deterministic_score": 50.0, "mandatory_skills": [],
    }
    # eligible_resumes preserves [weak_resume, strong_resume] order, so
    # _evaluate_one (and therefore _combine_deterministic_score) runs for
    # weak_resume first, strong_resume second - a plain ordered list is
    # sufficient and far less fragile than branching on call args.
    candidate_scoring_service._combine_deterministic_score.side_effect = [(40.0, False), (85.0, True)]

    service = make_service(
        resume_repo=resume_repo, jd_repo=jd_repo, candidate_scoring_service=candidate_scoring_service,
    )

    result = service.select_resume_for_campaign(uuid4(), _campaign())

    assert result.selection_method == SelectionMethod.COMPARED
    assert result.selected_resume is strong_resume
    assert len(result.evaluated_resumes) == 2
    selected_entries = [e for e in result.evaluated_resumes if e.is_selected]
    assert len(selected_entries) == 1
    assert selected_entries[0].resume is strong_resume


def test_falls_back_to_deterministic_only_when_jd_has_no_embedding():
    resume_a = _resume()
    resume_b = _resume()
    resume_repo = MagicMock()
    resume_repo.get_all_versions_by_candidate.return_value = [resume_a, resume_b]
    _with_embedding_map(resume_repo, {resume_a.id: _embedding(), resume_b.id: _embedding()})

    jd_repo = MagicMock()
    jd_repo.get_by_id.return_value = _job_description()
    jd_repo.get_embedding_by_jd_id.return_value = None  # No JD embedding at all.

    candidate_scoring_service = MagicMock()
    candidate_scoring_service.build_mandatory_skill_breakdown.return_value = {
        "deterministic_score": 60.0, "mandatory_skills": [],
    }
    candidate_scoring_service._combine_deterministic_score.side_effect = [(60.0, False), (90.0, True)]

    service = make_service(
        resume_repo=resume_repo, jd_repo=jd_repo, candidate_scoring_service=candidate_scoring_service,
    )

    result = service.select_resume_for_campaign(uuid4(), _campaign())

    resume_repo.compute_semantic_similarity.assert_not_called()
    for entry in result.evaluated_resumes:
        assert entry.semantic_score is None
        assert entry.semantic_passed is None
        # weight_semantic collapses to 0 -> selection_score == deterministic_score.
        assert entry.selection_score == entry.deterministic_score
    assert result.selected_resume is resume_b


def test_resume_specific_semantic_mismatch_contributes_zero_not_a_failure():
    """A per-resume embedding_model_version_id mismatch (compute_semantic_similarity
    returns None for just this one resume) must not fail the whole comparison."""
    resume_a = _resume()
    resume_b = _resume()
    resume_repo = MagicMock()
    resume_repo.get_all_versions_by_candidate.return_value = [resume_a, resume_b]
    _with_embedding_map(resume_repo, {resume_a.id: _embedding(), resume_b.id: _embedding()})
    resume_repo.compute_semantic_similarity.side_effect = lambda resume_id, jd_id: (
        None if resume_id == resume_a.id else 0.80
    )

    jd_repo = MagicMock()
    jd_repo.get_by_id.return_value = _job_description()
    jd_repo.get_embedding_by_jd_id.return_value = SimpleNamespace(id=uuid4())

    candidate_scoring_service = MagicMock()
    candidate_scoring_service.build_mandatory_skill_breakdown.return_value = {
        "deterministic_score": 70.0, "mandatory_skills": [],
    }
    candidate_scoring_service._combine_deterministic_score.return_value = (70.0, True)

    service = make_service(
        resume_repo=resume_repo, jd_repo=jd_repo, candidate_scoring_service=candidate_scoring_service,
    )

    result = service.select_resume_for_campaign(uuid4(), _campaign())

    entry_a = next(e for e in result.evaluated_resumes if e.resume is resume_a)
    assert entry_a.semantic_score is None
    assert entry_a.semantic_passed is None
    # Same deterministic score but resume_b has a real semantic bonus -> resume_b wins.
    assert result.selected_resume is resume_b


def test_negative_similarity_is_clamped_to_zero():
    resume = _resume()
    resume_repo = MagicMock()
    resume_repo.compute_semantic_similarity.return_value = -0.05
    campaign = _campaign()

    service = make_service(resume_repo=resume_repo)

    score, passed = service._score_semantic(resume, campaign)

    assert score == 0.0
    assert passed is False


# ----------------------------------------------------------------------
# Weight renormalization
# ----------------------------------------------------------------------

def test_selection_weights_renormalize_deterministic_and_semantic_to_100():
    campaign = _campaign(weight_deterministic=30.0, weight_semantic=40.0)

    weight_det, weight_sem = ResumeSelectionService._selection_weights(campaign, jd_has_embedding=True)

    assert weight_det == pytest.approx(30 / 70 * 100)
    assert weight_sem == pytest.approx(40 / 70 * 100)
    assert weight_det + weight_sem == pytest.approx(100.0)


def test_selection_weights_fall_back_to_full_deterministic_when_jd_has_no_embedding():
    campaign = _campaign(weight_deterministic=30.0, weight_semantic=40.0)

    weight_det, weight_sem = ResumeSelectionService._selection_weights(campaign, jd_has_embedding=False)

    assert weight_det == 100.0
    assert weight_sem == 0.0


def test_selection_weights_fall_back_when_both_weights_are_zero():
    campaign = _campaign(weight_deterministic=0.0, weight_semantic=0.0)

    weight_det, weight_sem = ResumeSelectionService._selection_weights(campaign, jd_has_embedding=True)

    assert weight_det == 100.0
    assert weight_sem == 0.0
