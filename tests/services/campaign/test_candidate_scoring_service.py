from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.services.campaign.candidate_scoring_service import CandidateScoringService

JD_ID = uuid4()
RESUME_ID = uuid4()


def _skill(canonical_skill_id):
    return SimpleNamespace(canonical_skill_id=canonical_skill_id)


def make_service(mandatory_skill_ids, candidate_skill_ids):
    """
    Builds a CandidateScoringService backed by a mocked SkillRepository.
    mandatory_skill_ids / candidate_skill_ids are plain lists of canonical
    skill ids (or None), one per row returned by the repository - duplicates
    are passed through as-is so tests can exercise de-duplication.
    """
    repository = MagicMock()
    repository.get_mandatory_jd_skills.return_value = [_skill(sid) for sid in mandatory_skill_ids]
    repository.get_candidate_normalized_skills.return_value = [_skill(sid) for sid in candidate_skill_ids]
    return CandidateScoringService(repository)


def test_all_mandatory_skills_matched_scores_100_percent():
    a, b, c = uuid4(), uuid4(), uuid4()
    service = make_service(mandatory_skill_ids=[a, b, c], candidate_skill_ids=[a, b, c])

    score, passed = service.calculate_deterministic_score(JD_ID, RESUME_ID, deterministic_threshold=70.0)

    assert score == 100.0
    assert passed is True


def test_partial_match_computes_proportional_score():
    a, b, c, d = uuid4(), uuid4(), uuid4(), uuid4()
    service = make_service(mandatory_skill_ids=[a, b, c, d], candidate_skill_ids=[a, b])

    score, passed = service.calculate_deterministic_score(JD_ID, RESUME_ID, deterministic_threshold=70.0)

    assert score == 50.0
    assert passed is False  # 50 < 70


def test_no_matching_skills_scores_0_percent():
    a, b = uuid4(), uuid4()
    other = uuid4()
    service = make_service(mandatory_skill_ids=[a, b], candidate_skill_ids=[other])

    score, passed = service.calculate_deterministic_score(JD_ID, RESUME_ID, deterministic_threshold=70.0)

    assert score == 0.0
    assert passed is False


def test_no_mandatory_skills_configured_auto_passes():
    service = make_service(mandatory_skill_ids=[], candidate_skill_ids=[uuid4()])

    score, passed = service.calculate_deterministic_score(JD_ID, RESUME_ID, deterministic_threshold=70.0)

    assert score == 100.0
    assert passed is True


def test_duplicate_candidate_skills_do_not_inflate_score():
    a, b = uuid4(), uuid4()
    # 'a' appears 3 times (e.g. matched from multiple raw-text extractions);
    # only 2 unique mandatory skills exist, so the score must still be
    # capped at matched-unique/total-unique, never exceeding 100%.
    service = make_service(mandatory_skill_ids=[a, b], candidate_skill_ids=[a, a, a, b])

    score, passed = service.calculate_deterministic_score(JD_ID, RESUME_ID, deterministic_threshold=70.0)

    assert score == 100.0
    assert passed is True


def test_duplicate_mandatory_skills_do_not_distort_denominator():
    a, b = uuid4(), uuid4()
    # 'a' duplicated among mandatory rows must not inflate the denominator
    # to 3 - the true unique mandatory-skill count is 2.
    service = make_service(mandatory_skill_ids=[a, a, b], candidate_skill_ids=[a])

    score, passed = service.calculate_deterministic_score(JD_ID, RESUME_ID, deterministic_threshold=70.0)

    assert score == 50.0
    assert passed is False


def test_candidate_skills_with_null_canonical_id_are_ignored():
    a, b = uuid4(), uuid4()
    # A candidate skill that failed normalization (canonical_skill_id=None)
    # must never participate in matching.
    service = make_service(mandatory_skill_ids=[a, b], candidate_skill_ids=[a, None])

    score, passed = service.calculate_deterministic_score(JD_ID, RESUME_ID, deterministic_threshold=70.0)

    assert score == 50.0
    assert passed is False


@pytest.mark.parametrize(
    "threshold, expected_passed",
    [
        (49.99, True),   # just below the score -> passes
        (50.00, True),   # exactly equal to the score -> passes (inclusive boundary)
        (50.01, False),  # just above the score -> fails
    ],
)
def test_deterministic_passed_uses_inclusive_threshold_boundary(threshold, expected_passed):
    a, b = uuid4(), uuid4()
    service = make_service(mandatory_skill_ids=[a, b], candidate_skill_ids=[a])

    score, passed = service.calculate_deterministic_score(JD_ID, RESUME_ID, deterministic_threshold=threshold)

    assert score == 50.0
    assert passed is expected_passed


def test_matching_uses_canonical_skill_id_not_object_identity():
    # Distinct row objects sharing the same canonical_skill_id must still
    # match - matching is keyed purely on canonical_skill_id.
    shared_id = uuid4()
    service = make_service(mandatory_skill_ids=[shared_id], candidate_skill_ids=[shared_id])

    score, passed = service.calculate_deterministic_score(JD_ID, RESUME_ID, deterministic_threshold=100.0)

    assert score == 100.0
    assert passed is True
