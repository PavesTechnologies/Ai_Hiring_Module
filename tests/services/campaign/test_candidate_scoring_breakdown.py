from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.services.campaign.candidate_scoring_service import (
    CandidateScoringService,
    MandatorySkillMatchType,
)

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


def _ontology_skill(skill_id, parent_skill_id=None, embedding=None):
    return SimpleNamespace(id=skill_id, parent_skill_id=parent_skill_id, embedding=embedding)


def _candidate_skill(canonical_skill_id, scoring_weight, match_tier="EXACT", confidence=1.0):
    return SimpleNamespace(
        canonical_skill_id=canonical_skill_id,
        scoring_weight=scoring_weight,
        match_tier=match_tier,
        confidence=confidence,
    )


def make_service(
    coverage_rows,
    candidate_skills=None,
    children_map=None,
    skill_by_id_map=None,
    config=None,
    semantic_match_result=None,
    with_campaign_candidate_repository=True,
    preferred_coverage_rows=None,
):
    skill_repository = MagicMock()

    def _coverage_side_effect(jd_id, resume_id, mandatory=True):
        return coverage_rows if mandatory else (preferred_coverage_rows or [])

    skill_repository.get_mandatory_skill_coverage.side_effect = _coverage_side_effect
    skill_repository.get_candidate_normalized_skills.return_value = candidate_skills or []
    skill_repository.find_best_semantic_match.return_value = semantic_match_result

    children_map = children_map or {}
    skill_by_id_map = skill_by_id_map or {}
    skill_ontology_repository = MagicMock()
    skill_ontology_repository.get_children.side_effect = lambda skill_id: children_map.get(skill_id, [])
    skill_ontology_repository.get_skill_by_id.side_effect = lambda skill_id: skill_by_id_map.get(skill_id)

    config_repository = MagicMock()
    config_repository.get_configs_by_keys.return_value = config or {}

    campaign_candidate_repository = MagicMock() if with_campaign_candidate_repository else None

    service = CandidateScoringService(
        skill_repository, skill_ontology_repository, config_repository, campaign_candidate_repository,
    )
    return service, campaign_candidate_repository


# ---------------------------------------------------------------- EXACT


def test_exact_match_scores_full_multiplier_and_contribution():
    a = uuid4()
    rows = [_coverage_row(a, weight=50.0, candidate_scoring_weight=0.9, match_tier="EXACT", confidence=1.0)]
    service, _ = make_service(rows)

    breakdown = service.build_mandatory_skill_breakdown(JD_ID, RESUME_ID)
    entry = breakdown["mandatory_skills"][0]

    assert entry["match_type"] == MandatorySkillMatchType.EXACT.value
    assert entry["hierarchy_score_multiplier"] == 1.0
    assert entry["contribution"] == round(50.0 * 0.9 * 1.0, 4)
    assert breakdown["mandatory_coverage_pct"] == 100.0


# ---------------------------------------------------------------- CHILD


def test_child_match_uses_highest_scoring_weight_among_multiple_children():
    mandatory_id = uuid4()
    child_low, child_high = uuid4(), uuid4()
    rows = [_coverage_row(mandatory_id, weight=100.0, candidate_scoring_weight=None)]
    candidate_skills = [
        _candidate_skill(child_low, scoring_weight=0.6),
        _candidate_skill(child_high, scoring_weight=0.9),
    ]
    children_map = {mandatory_id: [_ontology_skill(child_low), _ontology_skill(child_high)]}
    service, _ = make_service(rows, candidate_skills, children_map=children_map)

    breakdown = service.build_mandatory_skill_breakdown(JD_ID, RESUME_ID)
    entry = breakdown["mandatory_skills"][0]

    assert entry["match_type"] == MandatorySkillMatchType.CHILD.value
    assert entry["hierarchy_score_multiplier"] == 0.7
    assert entry["candidate_scoring_weight"] == 0.9  # the higher of the two children
    assert entry["contribution"] == round(100.0 * 0.9 * 0.7, 4)
    assert breakdown["mandatory_coverage_pct"] == 100.0


def test_direct_child_takes_precedence_over_grandchild():
    mandatory_id, child_id, grandchild_id = uuid4(), uuid4(), uuid4()
    rows = [_coverage_row(mandatory_id, weight=100.0, candidate_scoring_weight=None)]
    # Candidate has BOTH a child-level and a grandchild-level skill.
    candidate_skills = [
        _candidate_skill(child_id, scoring_weight=0.5),
        _candidate_skill(grandchild_id, scoring_weight=0.99),
    ]
    children_map = {
        mandatory_id: [_ontology_skill(child_id)],
        child_id: [_ontology_skill(grandchild_id)],
    }
    service, _ = make_service(rows, candidate_skills, children_map=children_map)

    breakdown = service.build_mandatory_skill_breakdown(JD_ID, RESUME_ID)
    entry = breakdown["mandatory_skills"][0]

    # Direct child wins even though the grandchild has a higher scoring_weight.
    assert entry["match_type"] == MandatorySkillMatchType.CHILD.value
    assert entry["candidate_scoring_weight"] == 0.5


# ---------------------------------------------------------------- GRANDCHILD


def test_grandchild_match_uses_configured_multiplier():
    mandatory_id, child_id, grandchild_low, grandchild_high = uuid4(), uuid4(), uuid4(), uuid4()
    rows = [_coverage_row(mandatory_id, weight=100.0, candidate_scoring_weight=None)]
    candidate_skills = [
        _candidate_skill(grandchild_low, scoring_weight=0.4),
        _candidate_skill(grandchild_high, scoring_weight=0.8),
    ]
    children_map = {
        mandatory_id: [_ontology_skill(child_id)],
        child_id: [_ontology_skill(grandchild_low), _ontology_skill(grandchild_high)],
    }
    service, _ = make_service(
        rows, candidate_skills, children_map=children_map,
        config={"HIERARCHY_GRANDCHILD_MULTIPLIER": "0.55"},
    )

    breakdown = service.build_mandatory_skill_breakdown(JD_ID, RESUME_ID)
    entry = breakdown["mandatory_skills"][0]

    assert entry["match_type"] == MandatorySkillMatchType.GRANDCHILD.value
    assert entry["hierarchy_score_multiplier"] == 0.55
    assert entry["candidate_scoring_weight"] == 0.8
    assert entry["contribution"] == round(100.0 * 0.8 * 0.55, 4)


def test_grandchild_multiplier_defaults_to_0_5_when_unconfigured():
    mandatory_id, child_id, grandchild_id = uuid4(), uuid4(), uuid4()
    rows = [_coverage_row(mandatory_id, weight=100.0, candidate_scoring_weight=None)]
    candidate_skills = [_candidate_skill(grandchild_id, scoring_weight=1.0)]
    children_map = {mandatory_id: [_ontology_skill(child_id)], child_id: [_ontology_skill(grandchild_id)]}
    service, _ = make_service(rows, candidate_skills, children_map=children_map, config={})

    breakdown = service.build_mandatory_skill_breakdown(JD_ID, RESUME_ID)

    assert breakdown["mandatory_skills"][0]["hierarchy_score_multiplier"] == 0.5


# ---------------------------------------------------------------- SIBLING


def test_sibling_match_excludes_self_and_uses_highest_scoring_weight():
    mandatory_id, parent_id, sibling_low, sibling_high = uuid4(), uuid4(), uuid4(), uuid4()
    rows = [_coverage_row(mandatory_id, weight=100.0, candidate_scoring_weight=None)]
    candidate_skills = [
        _candidate_skill(sibling_low, scoring_weight=0.3),
        _candidate_skill(sibling_high, scoring_weight=0.7),
    ]
    skill_by_id_map = {mandatory_id: _ontology_skill(mandatory_id, parent_skill_id=parent_id)}
    children_map = {
        mandatory_id: [],  # no children of its own
        parent_id: [
            _ontology_skill(mandatory_id, parent_skill_id=parent_id),  # itself - must be excluded
            _ontology_skill(sibling_low, parent_skill_id=parent_id),
            _ontology_skill(sibling_high, parent_skill_id=parent_id),
        ],
    }
    service, _ = make_service(rows, candidate_skills, children_map=children_map, skill_by_id_map=skill_by_id_map)

    breakdown = service.build_mandatory_skill_breakdown(JD_ID, RESUME_ID)
    entry = breakdown["mandatory_skills"][0]

    assert entry["match_type"] == MandatorySkillMatchType.SIBLING.value
    assert entry["hierarchy_score_multiplier"] == 0.4
    assert entry["candidate_scoring_weight"] == 0.7
    assert "sibling_skip_reason" not in entry


def test_sibling_skipped_for_root_skill_records_reason():
    mandatory_id = uuid4()
    rows = [_coverage_row(mandatory_id, weight=100.0, candidate_scoring_weight=None)]
    skill_by_id_map = {mandatory_id: _ontology_skill(mandatory_id, parent_skill_id=None)}
    service, _ = make_service(rows, children_map={mandatory_id: []}, skill_by_id_map=skill_by_id_map)

    breakdown = service.build_mandatory_skill_breakdown(JD_ID, RESUME_ID)
    entry = breakdown["mandatory_skills"][0]

    assert entry["match_type"] == MandatorySkillMatchType.MISSING.value
    assert entry["sibling_skip_reason"] == "ROOT_SKILL_NO_PARENT"


def test_sibling_skipped_when_skill_not_found_records_reason():
    mandatory_id = uuid4()
    rows = [_coverage_row(mandatory_id, weight=100.0, candidate_scoring_weight=None)]
    service, _ = make_service(rows, children_map={mandatory_id: []}, skill_by_id_map={})

    breakdown = service.build_mandatory_skill_breakdown(JD_ID, RESUME_ID)
    entry = breakdown["mandatory_skills"][0]

    assert entry["match_type"] == MandatorySkillMatchType.MISSING.value
    assert entry["sibling_skip_reason"] == "SIBLING_SKIPPED"


def test_child_or_grandchild_match_preempts_sibling_tier():
    mandatory_id, child_id, parent_id, sibling_id = uuid4(), uuid4(), uuid4(), uuid4()
    rows = [_coverage_row(mandatory_id, weight=100.0, candidate_scoring_weight=None)]
    # Candidate has both a direct child AND a sibling-eligible skill.
    candidate_skills = [
        _candidate_skill(child_id, scoring_weight=0.5),
        _candidate_skill(sibling_id, scoring_weight=0.99),
    ]
    skill_by_id_map = {mandatory_id: _ontology_skill(mandatory_id, parent_skill_id=parent_id)}
    children_map = {
        mandatory_id: [_ontology_skill(child_id)],
        parent_id: [_ontology_skill(sibling_id, parent_skill_id=parent_id)],
    }
    service, _ = make_service(rows, candidate_skills, children_map=children_map, skill_by_id_map=skill_by_id_map)

    breakdown = service.build_mandatory_skill_breakdown(JD_ID, RESUME_ID)
    entry = breakdown["mandatory_skills"][0]

    # CHILD wins even though the sibling candidate has a higher scoring_weight.
    assert entry["match_type"] == MandatorySkillMatchType.CHILD.value


# ---------------------------------------------------------------- SEMANTIC


def test_semantic_match_when_similarity_meets_threshold():
    mandatory_id, semantic_id = uuid4(), uuid4()
    rows = [_coverage_row(mandatory_id, weight=100.0, candidate_scoring_weight=None)]
    candidate_skills = [_candidate_skill(semantic_id, scoring_weight=0.6)]
    skill_by_id_map = {
        mandatory_id: _ontology_skill(mandatory_id, parent_skill_id=None, embedding=[0.1, 0.2, 0.3]),
    }
    service, _ = make_service(
        rows, candidate_skills, children_map={mandatory_id: []}, skill_by_id_map=skill_by_id_map,
        config={"HIERARCHY_SEMANTIC_ONLY_THRESHOLD": "0.75"},
        semantic_match_result=(semantic_id, 0.82),
    )

    breakdown = service.build_mandatory_skill_breakdown(JD_ID, RESUME_ID)
    entry = breakdown["mandatory_skills"][0]

    assert entry["match_type"] == MandatorySkillMatchType.SEMANTIC.value
    assert entry["hierarchy_score_multiplier"] == 0.2
    assert entry["semantic_similarity"] == 0.82
    assert entry["candidate_scoring_weight"] == 0.6
    assert breakdown["semantic_tier_available"] is True


def test_semantic_match_below_threshold_stays_missing():
    mandatory_id, semantic_id = uuid4(), uuid4()
    rows = [_coverage_row(mandatory_id, weight=100.0, candidate_scoring_weight=None)]
    candidate_skills = [_candidate_skill(semantic_id, scoring_weight=0.6)]
    skill_by_id_map = {
        mandatory_id: _ontology_skill(mandatory_id, parent_skill_id=None, embedding=[0.1, 0.2, 0.3]),
    }
    service, _ = make_service(
        rows, candidate_skills, children_map={mandatory_id: []}, skill_by_id_map=skill_by_id_map,
        config={"HIERARCHY_SEMANTIC_ONLY_THRESHOLD": "0.75"},
        semantic_match_result=(semantic_id, 0.50),
    )

    breakdown = service.build_mandatory_skill_breakdown(JD_ID, RESUME_ID)
    entry = breakdown["mandatory_skills"][0]

    assert entry["match_type"] == MandatorySkillMatchType.MISSING.value


def test_semantic_tier_skipped_when_threshold_not_configured():
    mandatory_id, semantic_id = uuid4(), uuid4()
    rows = [_coverage_row(mandatory_id, weight=100.0, candidate_scoring_weight=None)]
    candidate_skills = [_candidate_skill(semantic_id, scoring_weight=0.6)]
    skill_by_id_map = {
        mandatory_id: _ontology_skill(mandatory_id, parent_skill_id=None, embedding=[0.1, 0.2, 0.3]),
    }
    skill_repository_semantic_call = MagicMock(return_value=(semantic_id, 0.99))
    service, _ = make_service(
        rows, candidate_skills, children_map={mandatory_id: []}, skill_by_id_map=skill_by_id_map,
        config={},  # no HIERARCHY_SEMANTIC_ONLY_THRESHOLD
    )

    breakdown = service.build_mandatory_skill_breakdown(JD_ID, RESUME_ID)
    entry = breakdown["mandatory_skills"][0]

    assert entry["match_type"] == MandatorySkillMatchType.MISSING.value
    assert breakdown["semantic_tier_available"] is False
    service.skill_repository.find_best_semantic_match.assert_not_called()


def test_semantic_tier_skipped_when_mandatory_skill_has_no_embedding():
    mandatory_id = uuid4()
    rows = [_coverage_row(mandatory_id, weight=100.0, candidate_scoring_weight=None)]
    skill_by_id_map = {mandatory_id: _ontology_skill(mandatory_id, parent_skill_id=None, embedding=None)}
    service, _ = make_service(
        rows, children_map={mandatory_id: []}, skill_by_id_map=skill_by_id_map,
        config={"HIERARCHY_SEMANTIC_ONLY_THRESHOLD": "0.75"},
    )

    breakdown = service.build_mandatory_skill_breakdown(JD_ID, RESUME_ID)

    assert breakdown["mandatory_skills"][0]["match_type"] == MandatorySkillMatchType.MISSING.value
    service.skill_repository.find_best_semantic_match.assert_not_called()


# ---------------------------------------------------------------- MISSING


def test_missing_when_no_tier_matches_has_zero_contribution():
    mandatory_id = uuid4()
    rows = [_coverage_row(mandatory_id, weight=100.0, candidate_scoring_weight=None)]
    skill_by_id_map = {mandatory_id: _ontology_skill(mandatory_id, parent_skill_id=None)}
    service, _ = make_service(rows, children_map={mandatory_id: []}, skill_by_id_map=skill_by_id_map)

    breakdown = service.build_mandatory_skill_breakdown(JD_ID, RESUME_ID)
    entry = breakdown["mandatory_skills"][0]

    assert entry["match_type"] == MandatorySkillMatchType.MISSING.value
    assert entry["hierarchy_score_multiplier"] == 0.0
    assert entry["contribution"] == 0.0
    assert breakdown["mandatory_coverage_pct"] == 0.0


def test_no_mandatory_skills_configured_yields_full_coverage():
    service, _ = make_service(coverage_rows=[])

    breakdown = service.build_mandatory_skill_breakdown(JD_ID, RESUME_ID)

    assert breakdown["mandatory_skills"] == []
    assert breakdown["mandatory_coverage_pct"] == 100.0


# ---------------------------------------------------------------- mixed coverage / T03


def test_mixed_tiers_all_count_as_covered_except_missing():
    exact_id, child_id, missing_id, child_of_missing = uuid4(), uuid4(), uuid4(), uuid4()
    rows = [
        _coverage_row(exact_id, weight=25.0, candidate_scoring_weight=1.0, match_tier="EXACT", confidence=1.0),
        _coverage_row(child_id, weight=25.0, candidate_scoring_weight=None),
        _coverage_row(missing_id, weight=25.0, candidate_scoring_weight=None),
    ]
    candidate_skills = [_candidate_skill(child_of_missing, scoring_weight=0.5)]
    children_map = {
        child_id: [_ontology_skill(child_of_missing)],
        missing_id: [],
    }
    skill_by_id_map = {missing_id: _ontology_skill(missing_id, parent_skill_id=None)}
    service, _ = make_service(
        rows, candidate_skills, children_map=children_map, skill_by_id_map=skill_by_id_map,
    )

    breakdown = service.build_mandatory_skill_breakdown(JD_ID, RESUME_ID)
    by_id = {e["canonical_skill_id"]: e for e in breakdown["mandatory_skills"]}

    assert by_id[str(exact_id)]["match_type"] == MandatorySkillMatchType.EXACT.value
    assert by_id[str(child_id)]["match_type"] == MandatorySkillMatchType.CHILD.value
    assert by_id[str(missing_id)]["match_type"] == MandatorySkillMatchType.MISSING.value
    # 2 of 3 covered (EXACT + CHILD), MISSING not covered.
    assert breakdown["mandatory_coverage_pct"] == round(2 / 3 * 100, 2)


# ---------------------------------------------------------------- deterministic_passed override (rule 12)


def test_deterministic_passed_false_when_any_mandatory_skill_missing_even_if_coverage_meets_threshold():
    a, b, c, d = uuid4(), uuid4(), uuid4(), uuid4()
    rows = [
        _coverage_row(a, weight=25.0, candidate_scoring_weight=1.0, match_tier="EXACT", confidence=1.0),
        _coverage_row(b, weight=25.0, candidate_scoring_weight=1.0, match_tier="EXACT", confidence=1.0),
        _coverage_row(c, weight=25.0, candidate_scoring_weight=1.0, match_tier="EXACT", confidence=1.0),
        _coverage_row(d, weight=25.0, candidate_scoring_weight=None),  # stays MISSING
    ]
    skill_by_id_map = {d: _ontology_skill(d, parent_skill_id=None)}
    service, campaign_candidate_repository = make_service(
        rows, children_map={d: []}, skill_by_id_map=skill_by_id_map,
    )
    campaign_candidate = SimpleNamespace(id=uuid4(), score_breakdown=None, deterministic_score=None, deterministic_passed=None)
    campaign_candidate_repository.get_by_id.return_value = campaign_candidate

    # 75% coverage clears a 50% threshold, but one mandatory skill is MISSING.
    breakdown = service.calculate_and_store_score_breakdown(campaign_candidate.id, JD_ID, RESUME_ID, deterministic_threshold=50.0)

    assert breakdown["mandatory_coverage_pct"] == 75.0
    assert breakdown["deterministic_passed"] is False
    assert campaign_candidate.deterministic_passed is False
    assert campaign_candidate.deterministic_score == 75.0


def test_deterministic_passed_true_when_all_covered_and_threshold_met():
    a, b = uuid4(), uuid4()
    rows = [
        _coverage_row(a, weight=50.0, candidate_scoring_weight=1.0, match_tier="EXACT", confidence=1.0),
        _coverage_row(b, weight=50.0, candidate_scoring_weight=1.0, match_tier="EXACT", confidence=1.0),
    ]
    service, campaign_candidate_repository = make_service(rows)
    campaign_candidate = SimpleNamespace(id=uuid4(), score_breakdown=None, deterministic_score=None, deterministic_passed=None)
    campaign_candidate_repository.get_by_id.return_value = campaign_candidate

    breakdown = service.calculate_and_store_score_breakdown(campaign_candidate.id, JD_ID, RESUME_ID, deterministic_threshold=70.0)

    assert breakdown["deterministic_passed"] is True
    assert campaign_candidate.deterministic_passed is True


# ---------------------------------------------------------------- persistence plumbing


def test_calculate_and_store_score_breakdown_persists_onto_campaign_candidate():
    a = uuid4()
    rows = [_coverage_row(a, weight=100.0, candidate_scoring_weight=1.0, match_tier="EXACT", confidence=1.0)]
    service, campaign_candidate_repository = make_service(rows)
    campaign_candidate = SimpleNamespace(id=uuid4(), score_breakdown=None, deterministic_score=None, deterministic_passed=None)
    campaign_candidate_repository.get_by_id.return_value = campaign_candidate

    breakdown = service.calculate_and_store_score_breakdown(campaign_candidate.id, JD_ID, RESUME_ID, deterministic_threshold=70.0)

    assert campaign_candidate.score_breakdown == breakdown
    campaign_candidate_repository.update.assert_called_once_with(campaign_candidate)
    campaign_candidate_repository.commit.assert_not_called()


def test_calculate_and_store_score_breakdown_requires_repository():
    service, _ = make_service(coverage_rows=[], with_campaign_candidate_repository=False)

    with pytest.raises(ValueError):
        service.calculate_and_store_score_breakdown(uuid4(), JD_ID, RESUME_ID, deterministic_threshold=70.0)


def test_calculate_and_store_score_breakdown_raises_when_campaign_candidate_missing():
    service, campaign_candidate_repository = make_service(coverage_rows=[])
    campaign_candidate_repository.get_by_id.return_value = None

    with pytest.raises(ValueError):
        service.calculate_and_store_score_breakdown(uuid4(), JD_ID, RESUME_ID, deterministic_threshold=70.0)


def test_build_mandatory_skill_breakdown_requires_hierarchy_repositories():
    from app.services.campaign.candidate_scoring_service import CandidateScoringService

    service = CandidateScoringService(MagicMock())  # no skill_ontology_repository / config_repository

    with pytest.raises(ValueError):
        service.build_mandatory_skill_breakdown(JD_ID, RESUME_ID)


# ---------------------------------------------------------------- M03-E05 S01 T02: preferred skill bonus


def test_preferred_skill_exact_match_computes_contribution():
    preferred_id = uuid4()
    preferred_rows = [_coverage_row(preferred_id, weight=40.0, candidate_scoring_weight=0.8, match_tier="EXACT", confidence=1.0)]
    # build_preferred_skill_breakdown only needs skill_repository - no mandatory rows at all.
    service, _ = make_service(coverage_rows=[], preferred_coverage_rows=preferred_rows)

    breakdown = service.build_preferred_skill_breakdown(JD_ID, RESUME_ID)
    entry = breakdown["preferred_skills"][0]

    assert entry["match_type"] == MandatorySkillMatchType.EXACT.value
    assert entry["hierarchy_score_multiplier"] == 1.0
    assert entry["contribution"] == round(40.0 * 0.8 * 1.0, 4)
    assert breakdown["preferred_bonus_score"] == round(40.0 * 0.8 * 1.0, 4)


def test_preferred_skill_no_match_contributes_zero():
    preferred_id = uuid4()
    preferred_rows = [_coverage_row(preferred_id, weight=40.0, candidate_scoring_weight=None)]
    service, _ = make_service(coverage_rows=[], preferred_coverage_rows=preferred_rows)

    breakdown = service.build_preferred_skill_breakdown(JD_ID, RESUME_ID)
    entry = breakdown["preferred_skills"][0]

    assert entry["match_type"] == MandatorySkillMatchType.MISSING.value
    assert entry["hierarchy_score_multiplier"] == 0.0
    assert entry["contribution"] == 0.0
    assert breakdown["preferred_bonus_score"] == 0.0


def test_multiple_preferred_matches_sum_into_bonus_score():
    a, b, c = uuid4(), uuid4(), uuid4()
    preferred_rows = [
        _coverage_row(a, weight=30.0, candidate_scoring_weight=1.0, match_tier="EXACT", confidence=1.0),
        _coverage_row(b, weight=20.0, candidate_scoring_weight=0.5, match_tier="EXACT", confidence=1.0),
        _coverage_row(c, weight=50.0, candidate_scoring_weight=None),  # unmatched - contributes 0
    ]
    service, _ = make_service(coverage_rows=[], preferred_coverage_rows=preferred_rows)

    breakdown = service.build_preferred_skill_breakdown(JD_ID, RESUME_ID)

    expected_bonus = round(30.0 * 1.0 * 1.0 + 20.0 * 0.5 * 1.0, 4)
    assert breakdown["preferred_bonus_score"] == expected_bonus
    assert len(breakdown["preferred_skills"]) == 3


def test_no_preferred_skills_configured_yields_zero_bonus():
    service, _ = make_service(coverage_rows=[], preferred_coverage_rows=[])

    breakdown = service.build_preferred_skill_breakdown(JD_ID, RESUME_ID)

    assert breakdown["preferred_skills"] == []
    assert breakdown["preferred_bonus_score"] == 0.0


def test_preferred_bonus_added_to_final_deterministic_score():
    mandatory_id, preferred_id = uuid4(), uuid4()
    mandatory_rows = [_coverage_row(mandatory_id, weight=100.0, candidate_scoring_weight=1.0, match_tier="EXACT", confidence=1.0)]
    preferred_rows = [_coverage_row(preferred_id, weight=10.0, candidate_scoring_weight=1.0, match_tier="EXACT", confidence=1.0)]
    service, campaign_candidate_repository = make_service(mandatory_rows, preferred_coverage_rows=preferred_rows)
    campaign_candidate = SimpleNamespace(id=uuid4(), score_breakdown=None, deterministic_score=None, deterministic_passed=None)
    campaign_candidate_repository.get_by_id.return_value = campaign_candidate

    breakdown = service.calculate_and_store_score_breakdown(campaign_candidate.id, JD_ID, RESUME_ID, deterministic_threshold=70.0)

    assert breakdown["mandatory_coverage_pct"] == 100.0
    assert breakdown["preferred_bonus_score"] == 10.0
    assert breakdown["deterministic_score"] == 110.0
    assert campaign_candidate.deterministic_score == 110.0


def test_preferred_bonus_does_not_affect_mandatory_coverage_or_passed_decision():
    mandatory_id, missing_id, preferred_id = uuid4(), uuid4(), uuid4()
    mandatory_rows = [
        _coverage_row(mandatory_id, weight=50.0, candidate_scoring_weight=1.0, match_tier="EXACT", confidence=1.0),
        _coverage_row(missing_id, weight=50.0, candidate_scoring_weight=None),
    ]
    preferred_rows = [_coverage_row(preferred_id, weight=100.0, candidate_scoring_weight=1.0, match_tier="EXACT", confidence=1.0)]
    skill_by_id_map = {missing_id: _ontology_skill(missing_id, parent_skill_id=None)}
    service, campaign_candidate_repository = make_service(
        mandatory_rows, children_map={missing_id: []}, skill_by_id_map=skill_by_id_map,
        preferred_coverage_rows=preferred_rows,
    )
    campaign_candidate = SimpleNamespace(id=uuid4(), score_breakdown=None, deterministic_score=None, deterministic_passed=None)
    campaign_candidate_repository.get_by_id.return_value = campaign_candidate

    # Mandatory coverage is only 50%, and a huge preferred bonus (100.0) is
    # available - deterministic_passed must still be False (a missing
    # mandatory skill overrides everything, per the existing, unchanged rule).
    breakdown = service.calculate_and_store_score_breakdown(campaign_candidate.id, JD_ID, RESUME_ID, deterministic_threshold=40.0)

    assert breakdown["mandatory_coverage_pct"] == 50.0
    assert breakdown["preferred_bonus_score"] == 100.0
    assert breakdown["deterministic_score"] == 150.0
    assert breakdown["deterministic_passed"] is False
    assert campaign_candidate.deterministic_passed is False


def test_mandatory_scoring_and_hierarchy_entries_unchanged_when_preferred_skills_present():
    """Regression guard: adding preferred-skill scoring must not alter a single
    mandatory_skills entry, mandatory_coverage_pct, or deterministic_passed."""
    a, b = uuid4(), uuid4()
    mandatory_rows = [
        _coverage_row(a, weight=50.0, candidate_scoring_weight=0.9, match_tier="EXACT", confidence=1.0),
        _coverage_row(b, weight=50.0, candidate_scoring_weight=None),
    ]
    skill_by_id_map = {b: _ontology_skill(b, parent_skill_id=None)}
    preferred_rows = [_coverage_row(uuid4(), weight=25.0, candidate_scoring_weight=1.0, match_tier="EXACT", confidence=1.0)]

    service_without_preferred, _ = make_service(mandatory_rows, children_map={b: []}, skill_by_id_map=skill_by_id_map)
    service_with_preferred, _ = make_service(
        mandatory_rows, children_map={b: []}, skill_by_id_map=skill_by_id_map, preferred_coverage_rows=preferred_rows,
    )

    breakdown_without = service_without_preferred.build_mandatory_skill_breakdown(JD_ID, RESUME_ID)
    breakdown_with = service_with_preferred.build_mandatory_skill_breakdown(JD_ID, RESUME_ID)

    assert breakdown_without["mandatory_skills"] == breakdown_with["mandatory_skills"]
    assert breakdown_without["mandatory_coverage_pct"] == breakdown_with["mandatory_coverage_pct"]
