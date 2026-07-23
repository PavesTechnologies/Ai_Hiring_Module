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


def _ontology_skill(skill_id, parent_skill_id=None, embedding=None, canonical_name=None, is_active=True):
    return SimpleNamespace(
        id=skill_id,
        parent_skill_id=parent_skill_id,
        embedding=embedding,
        canonical_name=canonical_name or f"skill-{skill_id}",
        is_active=is_active,
    )


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
    assert entry["skill_contribution"] == round(50.0 * 0.9 * 1.0, 4)
    assert breakdown["mandatory_coverage_pct"] == 100.0


def test_breakdown_entry_contains_all_t03_required_fields():
    """
    M03-E05 S05 T03: every mandatory_skills entry must carry canonical_name,
    mandatory, configured_weight, match_type, matched_candidate_skill_canonical_name,
    hierarchy_score_multiplier, candidate_scoring_weight, skill_contribution.
    """
    a = uuid4()
    rows = [_coverage_row(a, weight=50.0, candidate_scoring_weight=0.9, match_tier="EXACT", confidence=1.0)]
    skill_by_id_map = {a: _ontology_skill(a, canonical_name="Python")}
    service, _ = make_service(rows, skill_by_id_map=skill_by_id_map)

    breakdown = service.build_mandatory_skill_breakdown(JD_ID, RESUME_ID)
    entry = breakdown["mandatory_skills"][0]

    assert entry["canonical_name"] == "Python"
    assert entry["mandatory"] is True
    assert entry["configured_weight"] == 50.0
    assert entry["match_type"] == MandatorySkillMatchType.EXACT.value
    # EXACT match: the matched candidate skill IS the JD skill itself.
    assert entry["matched_candidate_skill_canonical_name"] == "Python"
    assert entry["hierarchy_score_multiplier"] == 1.0
    assert entry["candidate_scoring_weight"] == 0.9
    assert entry["skill_contribution"] == round(50.0 * 0.9 * 1.0, 4)


def test_child_match_reports_matched_candidate_skill_canonical_name():
    mandatory_id, child_id = uuid4(), uuid4()
    rows = [_coverage_row(mandatory_id, weight=100.0, candidate_scoring_weight=None)]
    candidate_skills = [_candidate_skill(child_id, scoring_weight=0.8)]
    children_map = {mandatory_id: [_ontology_skill(child_id, canonical_name="Django")]}
    skill_by_id_map = {mandatory_id: _ontology_skill(mandatory_id, canonical_name="Python")}
    service, _ = make_service(
        rows, candidate_skills, children_map=children_map, skill_by_id_map=skill_by_id_map,
    )

    breakdown = service.build_mandatory_skill_breakdown(JD_ID, RESUME_ID)
    entry = breakdown["mandatory_skills"][0]

    assert entry["canonical_name"] == "Python"
    assert entry["match_type"] == MandatorySkillMatchType.CHILD.value
    # The matched candidate skill is the CHILD ("Django"), distinct from the
    # JD skill itself ("Python") - this is the whole point of the field.
    assert entry["matched_candidate_skill_canonical_name"] == "Django"


def test_missing_skill_entry_has_no_matched_candidate_skill_name():
    mandatory_id = uuid4()
    rows = [_coverage_row(mandatory_id, weight=50.0, candidate_scoring_weight=None)]
    skill_by_id_map = {mandatory_id: _ontology_skill(mandatory_id, parent_skill_id=None, canonical_name="Rust")}
    service, _ = make_service(rows, skill_by_id_map=skill_by_id_map)

    breakdown = service.build_mandatory_skill_breakdown(JD_ID, RESUME_ID)
    entry = breakdown["mandatory_skills"][0]

    assert entry["match_type"] == MandatorySkillMatchType.MISSING.value
    assert entry["canonical_name"] == "Rust"
    assert entry["matched_candidate_skill_canonical_name"] is None


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
    assert entry["skill_contribution"] == round(100.0 * 0.9 * 0.7, 4)
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


def test_inactive_child_is_ignored_even_when_candidate_has_matching_skill():
    """
    S03-T01: an inactive (deactivated/deprecated) ontology skill must never
    count as a hierarchy match target, even if a stale candidate_skills row
    still points at it - falls through to MISSING since it's the only
    candidate skill available.
    """
    mandatory_id, child_id = uuid4(), uuid4()
    rows = [_coverage_row(mandatory_id, weight=100.0, candidate_scoring_weight=None)]
    candidate_skills = [_candidate_skill(child_id, scoring_weight=0.9)]
    children_map = {mandatory_id: [_ontology_skill(child_id, is_active=False)]}
    skill_by_id_map = {mandatory_id: _ontology_skill(mandatory_id, parent_skill_id=None)}
    service, _ = make_service(
        rows, candidate_skills, children_map=children_map, skill_by_id_map=skill_by_id_map,
    )

    breakdown = service.build_mandatory_skill_breakdown(JD_ID, RESUME_ID)
    entry = breakdown["mandatory_skills"][0]

    assert entry["match_type"] == MandatorySkillMatchType.MISSING.value


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
    assert entry["skill_contribution"] == round(100.0 * 0.8 * 0.55, 4)


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


def test_inactive_sibling_is_ignored():
    """S03-T01: same inactive-skill exclusion, for the SIBLING tier."""
    mandatory_id, parent_id, sibling_id = uuid4(), uuid4(), uuid4()
    rows = [_coverage_row(mandatory_id, weight=100.0, candidate_scoring_weight=None)]
    candidate_skills = [_candidate_skill(sibling_id, scoring_weight=0.7)]
    skill_by_id_map = {mandatory_id: _ontology_skill(mandatory_id, parent_skill_id=parent_id)}
    children_map = {
        mandatory_id: [],
        parent_id: [
            _ontology_skill(mandatory_id, parent_skill_id=parent_id),
            _ontology_skill(sibling_id, parent_skill_id=parent_id, is_active=False),
        ],
    }
    service, _ = make_service(rows, candidate_skills, children_map=children_map, skill_by_id_map=skill_by_id_map)

    breakdown = service.build_mandatory_skill_breakdown(JD_ID, RESUME_ID)
    entry = breakdown["mandatory_skills"][0]

    assert entry["match_type"] == MandatorySkillMatchType.MISSING.value


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
    assert entry["skill_contribution"] == 0.0
    assert breakdown["mandatory_coverage_pct"] == 0.0


def test_no_mandatory_skills_configured_yields_full_coverage():
    service, _ = make_service(coverage_rows=[])

    breakdown = service.build_mandatory_skill_breakdown(JD_ID, RESUME_ID)

    assert breakdown["mandatory_skills"] == []
    assert breakdown["mandatory_coverage_pct"] == 100.0


# ---------------------------------------------------------------- M07-E01 S04-T01: zero verified skills


def test_zero_verified_candidate_skills_forces_every_mandatory_skill_missing():
    """
    S04-T01: no candidate_skills with scoring_weight > 0 at all (distinct
    from a resume parse failure, which never reaches this service) - every
    mandatory JD skill must come back MISSING, deterministic_score must be
    0 (not 100, which is reserved for "no mandatory skills configured"),
    and NO_VERIFIED_SKILLS must be flagged in the breakdown.
    """
    a, b = uuid4(), uuid4()
    rows = [
        _coverage_row(a, weight=60.0, candidate_scoring_weight=None),
        _coverage_row(b, weight=40.0, candidate_scoring_weight=None),
    ]
    # No candidate_skills and no children/siblings configured anywhere -
    # every tier fails for both mandatory skills.
    service, _ = make_service(rows, candidate_skills=[])

    breakdown = service.build_mandatory_skill_breakdown(JD_ID, RESUME_ID)

    assert breakdown["NO_VERIFIED_SKILLS"] is True
    assert breakdown["deterministic_score"] == 0.0
    assert all(
        entry["match_type"] == MandatorySkillMatchType.MISSING.value
        for entry in breakdown["mandatory_skills"]
    )


def test_verified_candidate_skills_present_do_not_flag_no_verified_skills():
    a = uuid4()
    rows = [_coverage_row(a, weight=50.0, candidate_scoring_weight=0.9, match_tier="EXACT", confidence=1.0)]
    candidate_skills = [_candidate_skill(a, scoring_weight=0.9)]
    service, _ = make_service(rows, candidate_skills=candidate_skills)

    breakdown = service.build_mandatory_skill_breakdown(JD_ID, RESUME_ID)

    assert breakdown["NO_VERIFIED_SKILLS"] is False


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
    assert breakdown["deterministic_score"] == 75.0
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


# ---------------------------------------------------------------- M07-E02 S04: Combined Deterministic Score


def _experience_result(passed, score, applicable=True):
    return {
        "applicable": applicable, "skipped": False, "data_missing": False,
        "passed": passed, "score": score, "candidate_years": 3.0, "min_years": 5.0,
        "effective_min_years": 5.0,
    }


def _education_result(passed, score, applicable=True):
    return {
        "applicable": applicable, "skipped": False, "data_missing": False,
        "passed": passed, "score": score, "required_level": "BACHELOR",
        "candidate_level": "BACHELOR", "equivalent_experience_applied": False,
    }


def test_omitting_experience_and_education_leaves_skill_only_score_unchanged():
    """Backward compatibility: pre-M07-E02 callers pass neither argument."""
    a = uuid4()
    rows = [_coverage_row(a, weight=100.0, candidate_scoring_weight=0.9, match_tier="EXACT", confidence=1.0)]
    service, campaign_candidate_repository = make_service(rows)
    campaign_candidate = SimpleNamespace(id=uuid4(), score_breakdown=None, deterministic_score=None, deterministic_passed=None)
    campaign_candidate_repository.get_by_id.return_value = campaign_candidate

    breakdown = service.calculate_and_store_score_breakdown(campaign_candidate.id, JD_ID, RESUME_ID, deterministic_threshold=70.0)

    assert breakdown["deterministic_score"] == round(100.0 * 0.9 * 1.0, 2)
    assert "skill_deterministic_score" not in breakdown
    assert "experience_validation" not in breakdown
    assert "education_validation" not in breakdown


def test_combined_score_blends_skill_experience_education_with_default_weights():
    a = uuid4()
    rows = [_coverage_row(a, weight=100.0, candidate_scoring_weight=1.0, match_tier="EXACT", confidence=1.0)]
    service, campaign_candidate_repository = make_service(rows)
    campaign_candidate = SimpleNamespace(id=uuid4(), score_breakdown=None, deterministic_score=None, deterministic_passed=None)
    campaign_candidate_repository.get_by_id.return_value = campaign_candidate

    experience_result = _experience_result(passed=True, score=80.0)
    education_result = _education_result(passed=True, score=60.0)

    breakdown = service.calculate_and_store_score_breakdown(
        campaign_candidate.id, JD_ID, RESUME_ID, deterministic_threshold=70.0,
        experience_result=experience_result, education_result=education_result,
    )

    expected = round((100.0 * 0.70 + 80.0 * 0.15 + 60.0 * 0.15) / 1.0, 2)
    assert breakdown["deterministic_score"] == expected
    assert breakdown["skill_deterministic_score"] == 100.0
    assert breakdown["experience_validation"] == experience_result
    assert breakdown["education_validation"] == education_result
    assert campaign_candidate.deterministic_score == expected


def test_combined_score_renormalizes_when_experience_not_applicable():
    a = uuid4()
    rows = [_coverage_row(a, weight=100.0, candidate_scoring_weight=1.0, match_tier="EXACT", confidence=1.0)]
    service, campaign_candidate_repository = make_service(rows)
    campaign_candidate = SimpleNamespace(id=uuid4(), score_breakdown=None, deterministic_score=None, deterministic_passed=None)
    campaign_candidate_repository.get_by_id.return_value = campaign_candidate

    # Experience SKIPPED (JD has no minimum) - excluded from the blend
    # entirely, weight renormalized across skills + education only.
    experience_result = _experience_result(passed=True, score=100.0, applicable=False)
    education_result = _education_result(passed=True, score=60.0)

    breakdown = service.calculate_and_store_score_breakdown(
        campaign_candidate.id, JD_ID, RESUME_ID, deterministic_threshold=70.0,
        experience_result=experience_result, education_result=education_result,
    )

    expected = round((100.0 * 0.70 + 60.0 * 0.15) / (0.70 + 0.15), 2)
    assert breakdown["deterministic_score"] == expected


def test_combined_gate_fails_when_experience_fails_even_if_score_clears_threshold():
    """
    M07-E02 S04: the gate is an AND across skill/experience/education, not
    purely the blended score - a high blended score must not paper over an
    outright experience failure.
    """
    a = uuid4()
    rows = [_coverage_row(a, weight=100.0, candidate_scoring_weight=1.0, match_tier="EXACT", confidence=1.0)]
    service, campaign_candidate_repository = make_service(rows)
    campaign_candidate = SimpleNamespace(id=uuid4(), score_breakdown=None, deterministic_score=None, deterministic_passed=None)
    campaign_candidate_repository.get_by_id.return_value = campaign_candidate

    experience_result = _experience_result(passed=False, score=60.0)
    education_result = _education_result(passed=True, score=100.0)

    breakdown = service.calculate_and_store_score_breakdown(
        campaign_candidate.id, JD_ID, RESUME_ID, deterministic_threshold=70.0,
        experience_result=experience_result, education_result=education_result,
    )

    assert breakdown["deterministic_score"] >= 70.0  # score alone would pass
    assert breakdown["deterministic_passed"] is False  # but experience failed


def test_combined_score_uses_custom_score_weights_when_provided():
    a = uuid4()
    rows = [_coverage_row(a, weight=100.0, candidate_scoring_weight=1.0, match_tier="EXACT", confidence=1.0)]
    service, campaign_candidate_repository = make_service(rows)
    campaign_candidate = SimpleNamespace(id=uuid4(), score_breakdown=None, deterministic_score=None, deterministic_passed=None)
    campaign_candidate_repository.get_by_id.return_value = campaign_candidate

    experience_result = _experience_result(passed=True, score=0.0)
    education_result = _education_result(passed=True, score=0.0)
    custom_weights = {"skills": 0.5, "experience": 0.25, "education": 0.25}

    breakdown = service.calculate_and_store_score_breakdown(
        campaign_candidate.id, JD_ID, RESUME_ID, deterministic_threshold=70.0,
        experience_result=experience_result, education_result=education_result,
        score_weights=custom_weights,
    )

    assert breakdown["deterministic_score"] == round(100.0 * 0.5, 2)


# ---------------------------------------------------------------- threshold validated once, against the combined score only


def test_case1_low_skill_score_but_high_combined_score_passes():
    """
    Case 1: skill=65 alone would fail a threshold of 70, but blended with
    experience=100/education=100 the combined score clears 70. The
    threshold must be checked exactly once, against the combined score -
    never separately against the skill sub-score - so this must PASS.
    """
    a = uuid4()
    rows = [_coverage_row(a, weight=100.0, candidate_scoring_weight=0.65, match_tier="EXACT", confidence=1.0)]
    service, campaign_candidate_repository = make_service(rows)
    campaign_candidate = SimpleNamespace(id=uuid4(), score_breakdown=None, deterministic_score=None, deterministic_passed=None)
    campaign_candidate_repository.get_by_id.return_value = campaign_candidate

    experience_result = _experience_result(passed=True, score=100.0)
    education_result = _education_result(passed=True, score=100.0)

    breakdown = service.calculate_and_store_score_breakdown(
        campaign_candidate.id, JD_ID, RESUME_ID, deterministic_threshold=70.0,
        experience_result=experience_result, education_result=education_result,
    )

    assert breakdown["skill_deterministic_score"] == 65.0
    expected_combined = round(65.0 * 0.70 + 100.0 * 0.15 + 100.0 * 0.15, 2)
    assert breakdown["deterministic_score"] == expected_combined
    assert expected_combined >= 70.0
    assert breakdown["deterministic_passed"] is True


def test_case2_high_skill_score_but_experience_fails():
    """Case 2: skill=95, experience FAILS -> must FAIL regardless of score."""
    a = uuid4()
    rows = [_coverage_row(a, weight=100.0, candidate_scoring_weight=0.95, match_tier="EXACT", confidence=1.0)]
    service, campaign_candidate_repository = make_service(rows)
    campaign_candidate = SimpleNamespace(id=uuid4(), score_breakdown=None, deterministic_score=None, deterministic_passed=None)
    campaign_candidate_repository.get_by_id.return_value = campaign_candidate

    experience_result = _experience_result(passed=False, score=40.0)
    education_result = _education_result(passed=True, score=100.0)

    breakdown = service.calculate_and_store_score_breakdown(
        campaign_candidate.id, JD_ID, RESUME_ID, deterministic_threshold=70.0,
        experience_result=experience_result, education_result=education_result,
    )

    assert breakdown["deterministic_passed"] is False


def test_case3_one_mandatory_skill_missing_fails_regardless_of_combined_score():
    """Case 3: one mandatory skill MISSING -> must FAIL regardless of combined score."""
    a = uuid4()
    rows = [_coverage_row(a, weight=100.0, candidate_scoring_weight=None)]
    skill_by_id_map = {a: _ontology_skill(a, parent_skill_id=None)}
    service, campaign_candidate_repository = make_service(rows, children_map={a: []}, skill_by_id_map=skill_by_id_map)
    campaign_candidate = SimpleNamespace(id=uuid4(), score_breakdown=None, deterministic_score=None, deterministic_passed=None)
    campaign_candidate_repository.get_by_id.return_value = campaign_candidate

    experience_result = _experience_result(passed=True, score=100.0)
    education_result = _education_result(passed=True, score=100.0)

    breakdown = service.calculate_and_store_score_breakdown(
        campaign_candidate.id, JD_ID, RESUME_ID, deterministic_threshold=70.0,
        experience_result=experience_result, education_result=education_result,
    )

    assert breakdown["mandatory_skills"][0]["match_type"] == MandatorySkillMatchType.MISSING.value
    assert breakdown["deterministic_passed"] is False


def test_case4_combined_score_below_threshold_fails():
    """Case 4: combined score below threshold -> must FAIL."""
    a = uuid4()
    rows = [_coverage_row(a, weight=100.0, candidate_scoring_weight=0.5, match_tier="EXACT", confidence=1.0)]
    service, campaign_candidate_repository = make_service(rows)
    campaign_candidate = SimpleNamespace(id=uuid4(), score_breakdown=None, deterministic_score=None, deterministic_passed=None)
    campaign_candidate_repository.get_by_id.return_value = campaign_candidate

    experience_result = _experience_result(passed=True, score=40.0)
    education_result = _education_result(passed=True, score=40.0)

    breakdown = service.calculate_and_store_score_breakdown(
        campaign_candidate.id, JD_ID, RESUME_ID, deterministic_threshold=70.0,
        experience_result=experience_result, education_result=education_result,
    )

    expected_combined = round(50.0 * 0.70 + 40.0 * 0.15 + 40.0 * 0.15, 2)
    assert breakdown["deterministic_score"] == expected_combined
    assert expected_combined < 70.0
    assert breakdown["deterministic_passed"] is False


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
    assert entry["skill_contribution"] == round(40.0 * 0.8 * 1.0, 4)
    assert breakdown["preferred_skill_bonus"] == round(40.0 * 0.8 * 1.0, 4)


def test_preferred_skill_no_match_contributes_zero():
    preferred_id = uuid4()
    preferred_rows = [_coverage_row(preferred_id, weight=40.0, candidate_scoring_weight=None)]
    service, _ = make_service(coverage_rows=[], preferred_coverage_rows=preferred_rows)

    breakdown = service.build_preferred_skill_breakdown(JD_ID, RESUME_ID)
    entry = breakdown["preferred_skills"][0]

    assert entry["match_type"] == MandatorySkillMatchType.MISSING.value
    assert entry["hierarchy_score_multiplier"] == 0.0
    assert entry["skill_contribution"] == 0.0
    assert breakdown["preferred_skill_bonus"] == 0.0


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
    assert breakdown["preferred_skill_bonus"] == expected_bonus
    assert len(breakdown["preferred_skills"]) == 3


def test_no_preferred_skills_configured_yields_zero_bonus():
    service, _ = make_service(coverage_rows=[], preferred_coverage_rows=[])

    breakdown = service.build_preferred_skill_breakdown(JD_ID, RESUME_ID)

    assert breakdown["preferred_skills"] == []
    assert breakdown["preferred_skill_bonus"] == 0.0


def test_preferred_skill_bonus_is_stored_separately_and_never_added_to_deterministic_score():
    """
    M07: preferred skills must NOT contribute to deterministic_score.
    A large preferred_skill_bonus must have zero effect on the mandatory
    ratio-based score - it's stored in score_breakdown.preferred_skill_bonus
    purely for a future Composite Score to consume.
    """
    mandatory_id, preferred_id = uuid4(), uuid4()
    mandatory_rows = [_coverage_row(mandatory_id, weight=80.0, candidate_scoring_weight=1.0, match_tier="EXACT", confidence=1.0)]
    preferred_rows = [_coverage_row(preferred_id, weight=10.0, candidate_scoring_weight=1.0, match_tier="EXACT", confidence=1.0)]
    service, campaign_candidate_repository = make_service(mandatory_rows, preferred_coverage_rows=preferred_rows)
    campaign_candidate = SimpleNamespace(id=uuid4(), score_breakdown=None, deterministic_score=None, deterministic_passed=None)
    campaign_candidate_repository.get_by_id.return_value = campaign_candidate

    breakdown = service.calculate_and_store_score_breakdown(campaign_candidate.id, JD_ID, RESUME_ID, deterministic_threshold=70.0)

    # actual (80) / max (80) * 100 = 100.0 - a single EXACT-matched
    # mandatory skill always reaches its own full ratio, regardless of the
    # preferred skill sitting alongside it.
    assert breakdown["deterministic_score"] == 100.0
    assert breakdown["preferred_skill_bonus"] == 10.0
    assert campaign_candidate.deterministic_score == 100.0


def test_preferred_bonus_does_not_affect_mandatory_coverage_score_or_passed_decision():
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

    # Mandatory: actual=50 / max=100 * 100 = 50.0. A huge preferred bonus
    # (100.0) exists alongside it but must have NO effect whatsoever on
    # deterministic_score, mandatory_coverage_pct, or deterministic_passed -
    # no addition, no clamping, nothing.
    breakdown = service.calculate_and_store_score_breakdown(campaign_candidate.id, JD_ID, RESUME_ID, deterministic_threshold=40.0)

    assert breakdown["mandatory_coverage_pct"] == 50.0
    assert breakdown["deterministic_score"] == 50.0
    assert breakdown["preferred_skill_bonus"] == 100.0
    assert breakdown["deterministic_passed"] is False  # forced by the MISSING mandatory skill
    assert campaign_candidate.deterministic_score == 50.0
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


# ---------------------------------------------------------------- deterministic_score = weighted sum, not coverage %


def test_100_percent_exact_coverage_yields_full_weighted_score():
    a, b = uuid4(), uuid4()
    rows = [
        _coverage_row(a, weight=60.0, candidate_scoring_weight=1.0, match_tier="EXACT", confidence=1.0),
        _coverage_row(b, weight=40.0, candidate_scoring_weight=1.0, match_tier="EXACT", confidence=1.0),
    ]
    service, _ = make_service(rows)

    breakdown = service.build_mandatory_skill_breakdown(JD_ID, RESUME_ID)

    assert breakdown["mandatory_coverage_pct"] == 100.0
    assert breakdown["deterministic_score"] == round(60.0 * 1.0 * 1.0 + 40.0 * 1.0 * 1.0, 4)


def test_100_percent_sibling_coverage_gives_full_coverage_but_lower_weighted_score():
    """
    Every mandatory skill matches, so mandatory_coverage_pct is 100% - but
    every match is via SIBLING (0.4 multiplier), so the actual weighted
    score must come out far lower than 100. This is the exact scenario the
    old (buggy) deterministic_score = mandatory_coverage_pct formula got
    wrong.
    """
    mandatory_a, mandatory_b = uuid4(), uuid4()
    parent_a, parent_b = uuid4(), uuid4()
    sibling_a, sibling_b = uuid4(), uuid4()

    rows = [
        _coverage_row(mandatory_a, weight=50.0, candidate_scoring_weight=None),
        _coverage_row(mandatory_b, weight=50.0, candidate_scoring_weight=None),
    ]
    candidate_skills = [
        _candidate_skill(sibling_a, scoring_weight=1.0),
        _candidate_skill(sibling_b, scoring_weight=1.0),
    ]
    skill_by_id_map = {
        mandatory_a: _ontology_skill(mandatory_a, parent_skill_id=parent_a),
        mandatory_b: _ontology_skill(mandatory_b, parent_skill_id=parent_b),
    }
    children_map = {
        mandatory_a: [], mandatory_b: [],
        parent_a: [_ontology_skill(mandatory_a, parent_skill_id=parent_a), _ontology_skill(sibling_a, parent_skill_id=parent_a)],
        parent_b: [_ontology_skill(mandatory_b, parent_skill_id=parent_b), _ontology_skill(sibling_b, parent_skill_id=parent_b)],
    }
    service, _ = make_service(rows, candidate_skills, children_map=children_map, skill_by_id_map=skill_by_id_map)

    breakdown = service.build_mandatory_skill_breakdown(JD_ID, RESUME_ID)

    assert breakdown["mandatory_coverage_pct"] == 100.0
    for entry in breakdown["mandatory_skills"]:
        assert entry["match_type"] == MandatorySkillMatchType.SIBLING.value
    # weighted score = (50*1*0.4) + (50*1*0.4) = 40.0, far below the 100%
    # coverage figure.
    assert breakdown["deterministic_score"] == 40.0
    assert breakdown["deterministic_score"] < breakdown["mandatory_coverage_pct"]


def test_semantic_match_contributes_at_0_2_credit():
    mandatory_id, semantic_id = uuid4(), uuid4()
    rows = [_coverage_row(mandatory_id, weight=100.0, candidate_scoring_weight=None)]
    candidate_skills = [_candidate_skill(semantic_id, scoring_weight=1.0)]
    skill_by_id_map = {mandatory_id: _ontology_skill(mandatory_id, parent_skill_id=None, embedding=[0.1, 0.2, 0.3])}
    service, _ = make_service(
        rows, candidate_skills, children_map={mandatory_id: []}, skill_by_id_map=skill_by_id_map,
        config={"HIERARCHY_SEMANTIC_ONLY_THRESHOLD": "0.75"}, semantic_match_result=(semantic_id, 0.9),
    )

    breakdown = service.build_mandatory_skill_breakdown(JD_ID, RESUME_ID)

    assert breakdown["mandatory_skills"][0]["match_type"] == MandatorySkillMatchType.SEMANTIC.value
    assert breakdown["mandatory_coverage_pct"] == 100.0
    assert breakdown["deterministic_score"] == round(100.0 * 1.0 * 0.2, 4)


def test_missing_skill_zero_contribution_reduces_weighted_score_and_forces_fail():
    matched_id, missing_id = uuid4(), uuid4()
    rows = [
        _coverage_row(matched_id, weight=50.0, candidate_scoring_weight=1.0, match_tier="EXACT", confidence=1.0),
        _coverage_row(missing_id, weight=50.0, candidate_scoring_weight=None),
    ]
    skill_by_id_map = {missing_id: _ontology_skill(missing_id, parent_skill_id=None)}
    service, campaign_candidate_repository = make_service(rows, children_map={missing_id: []}, skill_by_id_map=skill_by_id_map)
    campaign_candidate = SimpleNamespace(id=uuid4(), score_breakdown=None, deterministic_score=None, deterministic_passed=None)
    campaign_candidate_repository.get_by_id.return_value = campaign_candidate

    # Weighted score (50.0) alone would clear a 40% threshold, but the
    # missing mandatory skill must still force a fail.
    breakdown = service.calculate_and_store_score_breakdown(campaign_candidate.id, JD_ID, RESUME_ID, deterministic_threshold=40.0)

    missing_entry = next(e for e in breakdown["mandatory_skills"] if e["canonical_skill_id"] == str(missing_id))
    assert missing_entry["skill_contribution"] == 0.0
    assert breakdown["deterministic_score"] == 50.0
    assert breakdown["deterministic_passed"] is False


def test_threshold_boundary_evaluated_against_weighted_score_not_coverage_pct():
    """
    The bug this ticket fixes: 100% mandatory_coverage_pct (via SIBLING
    matches) must NOT be enough to pass a 50% threshold when the actual
    weighted score is only 40%.
    """
    mandatory_a, mandatory_b = uuid4(), uuid4()
    parent_a, parent_b = uuid4(), uuid4()
    sibling_a, sibling_b = uuid4(), uuid4()

    rows = [
        _coverage_row(mandatory_a, weight=50.0, candidate_scoring_weight=None),
        _coverage_row(mandatory_b, weight=50.0, candidate_scoring_weight=None),
    ]
    candidate_skills = [
        _candidate_skill(sibling_a, scoring_weight=1.0),
        _candidate_skill(sibling_b, scoring_weight=1.0),
    ]
    skill_by_id_map = {
        mandatory_a: _ontology_skill(mandatory_a, parent_skill_id=parent_a),
        mandatory_b: _ontology_skill(mandatory_b, parent_skill_id=parent_b),
    }
    children_map = {
        mandatory_a: [], mandatory_b: [],
        parent_a: [_ontology_skill(mandatory_a, parent_skill_id=parent_a), _ontology_skill(sibling_a, parent_skill_id=parent_a)],
        parent_b: [_ontology_skill(mandatory_b, parent_skill_id=parent_b), _ontology_skill(sibling_b, parent_skill_id=parent_b)],
    }
    service, campaign_candidate_repository = make_service(
        rows, candidate_skills, children_map=children_map, skill_by_id_map=skill_by_id_map,
    )
    campaign_candidate = SimpleNamespace(id=uuid4(), score_breakdown=None, deterministic_score=None, deterministic_passed=None)
    campaign_candidate_repository.get_by_id.return_value = campaign_candidate

    breakdown = service.calculate_and_store_score_breakdown(campaign_candidate.id, JD_ID, RESUME_ID, deterministic_threshold=50.0)

    assert breakdown["mandatory_coverage_pct"] == 100.0
    assert breakdown["deterministic_score"] == 40.0
    assert breakdown["deterministic_passed"] is False  # 40.0 < 50.0 threshold, despite 100% coverage
    assert campaign_candidate.deterministic_passed is False


def test_final_score_stays_within_0_100_regardless_of_weight_magnitude():
    """
    deterministic_score = actual/max*100 is a ratio, not an absolute sum -
    it must land at exactly 100 for an all-EXACT match REGARDLESS of
    whether the underlying jd_skill weights happen to sum to 100, 3, or
    any other number (matching the real pipeline, where every skill now
    gets the same flat equal weight - see JDService._DEFAULT_JD_SKILL_WEIGHT
    - not a 100-point budget split).
    """
    a, b, c = uuid4(), uuid4(), uuid4()
    rows = [
        _coverage_row(a, weight=1.0, candidate_scoring_weight=1.0, match_tier="EXACT", confidence=1.0),
        _coverage_row(b, weight=1.0, candidate_scoring_weight=1.0, match_tier="EXACT", confidence=1.0),
        _coverage_row(c, weight=1.0, candidate_scoring_weight=1.0, match_tier="EXACT", confidence=1.0),
    ]
    service, campaign_candidate_repository = make_service(rows)
    campaign_candidate = SimpleNamespace(id=uuid4(), score_breakdown=None, deterministic_score=None, deterministic_passed=None)
    campaign_candidate_repository.get_by_id.return_value = campaign_candidate

    breakdown = service.calculate_and_store_score_breakdown(campaign_candidate.id, JD_ID, RESUME_ID, deterministic_threshold=70.0)

    # actual = 1+1+1 = 3, max = 1+1+1 = 3 -> 3/3*100 = 100.0, even though
    # the weights (1.0 each) sum to 3, not 100.
    assert breakdown["deterministic_score"] == 100.0
    assert campaign_candidate.deterministic_score == 100.0


def test_partial_match_ratio_independent_of_weight_magnitude():
    """Same proof as above, but with a partial (not 100%) match, and non-unit weights that don't sum to 100."""
    a, b = uuid4(), uuid4()
    rows = [
        _coverage_row(a, weight=7.0, candidate_scoring_weight=1.0, match_tier="EXACT", confidence=1.0),
        _coverage_row(b, weight=7.0, candidate_scoring_weight=None),  # MISSING
    ]
    skill_by_id_map = {b: _ontology_skill(b, parent_skill_id=None)}
    service, campaign_candidate_repository = make_service(rows, children_map={b: []}, skill_by_id_map=skill_by_id_map)
    campaign_candidate = SimpleNamespace(id=uuid4(), score_breakdown=None, deterministic_score=None, deterministic_passed=None)
    campaign_candidate_repository.get_by_id.return_value = campaign_candidate

    breakdown = service.calculate_and_store_score_breakdown(campaign_candidate.id, JD_ID, RESUME_ID, deterministic_threshold=40.0)

    # actual = 7 (matched) + 0 (missing) = 7, max = 7 + 7 = 14 -> 7/14*100 = 50.0
    assert breakdown["deterministic_score"] == 50.0
    assert breakdown["deterministic_passed"] is False  # MISSING mandatory skill forces fail
