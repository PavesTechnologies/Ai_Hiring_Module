from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.services.campaign.candidate_scoring_service import CandidateScoringService

JD_ID = uuid4()
RESUME_ID = uuid4()


def _coverage_row(canonical_skill_id, weight, candidate_scoring_weight, match_tier=None, confidence=None):
    """Mimics one row of SkillRepository.get_mandatory_skill_coverage's LEFT JOIN result."""
    return SimpleNamespace(
        canonical_skill_id=canonical_skill_id,
        weight=weight,
        mandatory=True,
        candidate_scoring_weight=candidate_scoring_weight,
        match_tier=match_tier,
        confidence=confidence,
    )


def make_service(coverage_rows, skill_by_id_map=None):
    """
    calculate_deterministic_score delegates entirely to
    build_mandatory_skill_breakdown (weighted SUM(skill_contribution)/
    SUM(configured_weight) x 100), so this only needs to stub the same
    LEFT JOIN that method reads - no candidate_skills_by_id/children/
    siblings/semantic fixtures are needed since these tests never exercise
    anything past the EXACT tier.
    """
    skill_repository = MagicMock()
    skill_repository.get_mandatory_skill_coverage.side_effect = (
        lambda jd_id, resume_id, mandatory=True: coverage_rows if mandatory else []
    )
    skill_repository.get_candidate_normalized_skills.return_value = []

    skill_by_id_map = skill_by_id_map or {}
    skill_ontology_repository = MagicMock()
    skill_ontology_repository.get_children.return_value = []
    skill_ontology_repository.get_skill_by_id.side_effect = lambda skill_id: skill_by_id_map.get(skill_id)

    config_repository = MagicMock()
    config_repository.get_configs_by_keys.return_value = {}

    return CandidateScoringService(skill_repository, skill_ontology_repository, config_repository)


def test_delegates_to_weighted_breakdown_not_a_match_count_ratio():
    """
    The historical bug: score used to be len(matched)/len(total)*100 (a
    pure count ratio, blind to weight magnitude). It must now equal
    build_mandatory_skill_breakdown's weighted SUM(skill_contribution)/
    SUM(configured_weight)*100 - proven here with two EXACT matches at
    very different weights and partial candidate_scoring_weight, where a
    count-based ratio (which would just be 100%, 2 of 2 matched) and the
    true weighted ratio diverge.
    """
    a, b = uuid4(), uuid4()
    rows = [
        _coverage_row(a, weight=90.0, candidate_scoring_weight=1.0, match_tier="EXACT", confidence=1.0),
        _coverage_row(b, weight=10.0, candidate_scoring_weight=0.5, match_tier="EXACT", confidence=1.0),
    ]
    service = make_service(rows)

    score, passed = service.calculate_deterministic_score(JD_ID, RESUME_ID, deterministic_threshold=70.0)

    # Count-based would give 100.0 (2 of 2 matched); weighted gives 95.0.
    assert score == round((90.0 * 1.0 + 10.0 * 0.5) / 100.0 * 100, 2)
    assert passed is True


def test_passed_requires_score_at_or_above_threshold():
    a = uuid4()
    rows = [_coverage_row(a, weight=100.0, candidate_scoring_weight=0.5, match_tier="EXACT", confidence=1.0)]
    service = make_service(rows)

    score, passed = service.calculate_deterministic_score(JD_ID, RESUME_ID, deterministic_threshold=70.0)

    assert score == 50.0
    assert passed is False


def test_passed_is_false_when_a_mandatory_skill_is_missing_even_if_score_meets_threshold():
    """
    Mandatory skills remain mandatory: the delegated any-MISSING gate must
    still block passed=True regardless of the weighted score.
    """
    a, b = uuid4(), uuid4()
    rows = [
        _coverage_row(a, weight=90.0, candidate_scoring_weight=1.0, match_tier="EXACT", confidence=1.0),
        _coverage_row(b, weight=10.0, candidate_scoring_weight=None),
    ]
    skill_by_id_map = {b: SimpleNamespace(canonical_name="B", parent_skill_id=None, embedding=None)}
    service = make_service(rows, skill_by_id_map=skill_by_id_map)

    score, passed = service.calculate_deterministic_score(JD_ID, RESUME_ID, deterministic_threshold=70.0)

    assert score == 90.0  # score alone would clear the threshold
    assert passed is False  # but skill 'b' is MISSING


def test_no_mandatory_skills_configured_auto_passes():
    service = make_service(coverage_rows=[])

    score, passed = service.calculate_deterministic_score(JD_ID, RESUME_ID, deterministic_threshold=70.0)

    assert score == 100.0
    assert passed is True


@pytest.mark.parametrize(
    "threshold, expected_passed",
    [
        (49.99, True),
        (50.00, True),
        (50.01, False),
    ],
)
def test_passed_uses_inclusive_threshold_boundary(threshold, expected_passed):
    a = uuid4()
    rows = [_coverage_row(a, weight=100.0, candidate_scoring_weight=0.5, match_tier="EXACT", confidence=1.0)]
    service = make_service(rows)

    score, passed = service.calculate_deterministic_score(JD_ID, RESUME_ID, deterministic_threshold=threshold)

    assert score == 50.0
    assert passed is expected_passed


def test_requires_hierarchy_repositories():
    """Delegation means the same guard build_mandatory_skill_breakdown has now applies here too."""
    service = CandidateScoringService(MagicMock())  # no skill_ontology_repository / config_repository

    with pytest.raises(ValueError):
        service.calculate_deterministic_score(JD_ID, RESUME_ID, deterministic_threshold=70.0)
