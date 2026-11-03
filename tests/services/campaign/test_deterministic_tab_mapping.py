from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.campaign.campaign_candidate_service import CampaignCandidateService

"""
/deterministic tab mapping: education with no JD requirement is NOT_REQUIRED
(never a "no qualifying degree" placeholder), parts left out of the final
score say so, missing skills carry core/supporting, and failure reasons are
the deterministic layer's own - rebuilt from the breakdown, never stale or
from a later layer.
"""


def _skill(name, importance, missing):
    # v2's entry shape (retire v1, phase C)
    return {
        "canonical_name": name, "canonical_skill_id": name, "importance": importance,
        "match_type": "MISSING" if missing else "EXACT", "configured_weight": 1.0, "effective_weight": 1.0,
        "contribution": 0.0 if missing else 1.0, "recognition": 0.0 if missing else 1.0,
        "matched_skill_name": None if missing else name, "counts_for_core_gate": not missing,
        "confirmation_status": "NOT_FOUND" if missing else "CONFIRMED",
    }


def _breakdown(**overrides):
    b = {
        "mandatory_skills": [
            _skill("Program Management", "core", False), _skill("AI Governance", "core", True),
            _skill("Risk Management", "supporting", True),
        ],
        "preferred_skills": [],
        "scoring_version": "v2", "scoring_mode": "llm", "outcome": "REJECT",
        "core_skill_count": 2, "core_confirmed_count": 1, "missing_core_skill_count": 1,
        "configured_allowed_missing_core_skills": 3, "allowed_missing_core_skills": 0, "core_coverage_pct": 50.0,
        "deterministic_score": 21.0, "deterministic_threshold": 40.0, "score_threshold_rejects": False,
        "skill_score": 0.0, "functional_score": 54.0, "deterministic_passed": False,
        "checks": [
            {"check": "Core skills", "passed": True, "applies": True, "detail": "1 of 2 core skills confirmed"},
            {"check": "Experience", "passed": False, "applies": True, "detail": "8.6 years; minimum 10.0 required."},
            {"check": "Education", "passed": True, "applies": False},
        ],
        "components": [{"name": "skills", "score": 0.0, "weight": 0.7}, {"name": "functional", "score": 54.0,
                                                                         "weight": 0.3}],
        "experience_validation": {"applicable": True, "skipped": False, "data_missing": False, "passed": False,
                                  "score": 86.0, "candidate_years": 8.6, "min_years": 10.0, "effective_min_years": 10.0},
        "education_validation": {"applicable": False, "skipped": True, "data_missing": False, "passed": True,
                                 "score": 100.0, "required_level": None, "candidate_level": "MASTER",
                                 "required_degree_text": None, "candidate_degree_text": "Master of Science",
                                 "field_match": "NOT_REQUIRED"},
        "domain_capability_validation": {"applicable": True, "score": 54.0, "required": [], "preferred": []},
    }
    b.update(overrides)
    return b


def _build(breakdown, stored_reason="Missing required skills: everything | stale text"):
    service = CampaignCandidateService(MagicMock(), MagicMock(), MagicMock())
    service.config_repo = None
    candidate = SimpleNamespace(deterministic_breakdown=breakdown, screened_at=None)
    return service._build_deterministic_score_breakdown(candidate, rejection_reason=stored_reason)


def test_education_without_a_jd_requirement_is_not_required_not_a_placeholder():
    education = _build(_breakdown()).education_validation

    assert education.status == "NOT_REQUIRED"
    assert education.required_degree is None
    assert education.candidate_degree == "Master of Science"


def test_parts_left_out_of_the_final_score_are_flagged():
    calc = _build(_breakdown()).score_calculation

    assert (calc.experience_applicable, calc.education_applicable, calc.functional_applicable) == (True, False, True)


def test_failure_reasons_are_rebuilt_from_the_breakdown_not_the_stored_text():
    summary = _build(_breakdown()).summary

    # v2's own failed check, never the stored (possibly stale, or later-layer) text
    assert summary.failure_reasons == ["Experience: 8.6 years; minimum 10.0 required."]


def test_a_deterministic_pass_shows_no_failure_reason_even_if_a_later_layer_rejected():
    summary = _build(_breakdown(deterministic_passed=True, experience_validation=None),
                     stored_reason="Resume-to-job semantic similarity is 34.6%...").summary

    assert summary.failure_reasons == []
    assert summary.failure_reason is None


def test_missing_skills_carry_importance_and_an_honest_reason():
    missing = {m.skill: m for m in _build(_breakdown()).missing_mandatory_skills}

    assert missing["AI Governance"].importance == "core"
    assert "missing core-skill limit" in missing["AI Governance"].reason
    assert missing["Risk Management"].importance == "supporting"
    assert "lowers the score only" in missing["Risk Management"].reason


def test_core_counts_and_rule_are_exposed():
    response = _build(_breakdown())

    assert (response.summary.core_skills_matched, response.summary.core_skills_total) == (1, 2)
    assert (response.configuration.max_missing_core_skills, response.configuration.allowed_missing_core_skills) == (3, 0)
    assert (response.configuration.skills_weight, response.configuration.functional_weight) == (0.7, 0.3)
    assert [c.check for c in response.summary.decision_checks] == ["Core skills", "Experience", "Education"]
