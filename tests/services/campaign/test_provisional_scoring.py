"""
Unresolved JD unknown skills are scored provisionally in v1 and v2: tier
weight x 0.8 when found in the resume, 0 when not - in the skill score only,
never in a gate (core gate, coverage, missing-core count).
"""

from uuid import uuid4

import pytest

from app.services.campaign.scoring_v2 import (
    EvidenceInput, JDSkillInput, ProvisionalSkillInput, ScoringInputs, merge_config, score_v2,
)

CORE, SUP = uuid4(), uuid4()
NO_HIERARCHY = {"skills_by_id": {}, "children_by_parent": {}, "grandchildren_by_parent": {}}
JD = [JDSkillInput(CORE, "Program Management", True, 1.0, "core"),
      JDSkillInput(SUP, "Stakeholder Management", True, 1.0, "supporting")]
EVIDENCE = [EvidenceInput(CORE, None, ("SCAN",), "SCAN", "SCAN", 1.0, "line"),
            EvidenceInput(SUP, None, ("SCAN",), "SCAN", "SCAN", 1.0, "line")]
CONFIRMED = {str(CORE): {"status": "CONFIRMED"}}


def _provisional(tier, found):
    return ProvisionalSkillInput(uuid4(), f"Unknown {tier}", tier, found, "excerpt" if found else None)


def _v2(provisional):
    inputs = ScoringInputs(jd_skills=JD, evidence=EVIDENCE, confirmations=CONFIRMED, hierarchy=NO_HIERARCHY,
                           deterministic_threshold=40.0, allowed_missing_core=0, provisional_skills=provisional)
    return score_v2(inputs, merge_config(None))


# --- v2 ----------------------------------------------------------------------

def test_v2_found_provisional_skill_contributes_tier_weight_times_0_8():
    result = _v2([_provisional("SUPPORTING", True)])
    entry = result["provisional_skills"][0]
    assert entry["effective_weight"] == 1.0 and entry["quality"] == 0.8 and entry["contribution"] == 0.8
    # core 2 + supporting 1 found fully; provisional 0.8 of 1 -> 3.8 / 4
    assert result["required_skill_score"] == pytest.approx(95.0)


def test_v2_core_tier_weighs_double_and_unmatched_counts_zero():
    result = _v2([_provisional("CORE", False)])
    entry = result["provisional_skills"][0]
    assert entry["effective_weight"] == 2.0 and entry["contribution"] == 0.0 and entry["match_type"] == "MISSING"
    assert result["required_skill_score"] == pytest.approx(60.0)      # 3 / 5


def test_v2_provisional_skills_never_touch_a_gate():
    baseline, with_missing_core = _v2([]), _v2([_provisional("CORE", False), _provisional("CORE", False)])
    for key in ("core_skill_count", "core_confirmed_count", "missing_core_skill_count", "core_coverage_pct"):
        assert with_missing_core[key] == baseline[key]
    core_gate = next(c for c in with_missing_core["checks"] if c["check"] == "Core skills")
    assert core_gate["passed"] is True
    assert all(e["counts_for_core_gate"] is False for e in with_missing_core["provisional_skills"])


def test_v2_preferred_provisional_skill_goes_to_the_preferred_score():
    result = _v2([_provisional("PREFERRED", True)])
    assert result["preferred_skill_score"] == pytest.approx(80.0)
    assert result["required_skill_score"] == pytest.approx(100.0)


def test_v2_a_skill_that_could_not_be_scanned_is_excluded_not_counted_as_not_found():
    not_scanned = ProvisionalSkillInput(uuid4(), "Agentic Workflows", "CORE", False, None, scanned=False)
    # found=True with scanned=False must not earn credit either
    stale_hit = ProvisionalSkillInput(uuid4(), "Prompt Design", "SUPPORTING", True, "old", scanned=False)
    baseline, result = _v2([]), _v2([not_scanned, stale_hit])

    for key in ("required_skill_score", "preferred_skill_score", "skill_score", "deterministic_score"):
        assert result[key] == baseline[key]
    assert [e["match_type"] for e in result["provisional_skills"]] == ["NOT_SCANNED", "NOT_SCANNED"]
    assert all(e["scanned"] is False and e["contribution"] == 0.0 for e in result["provisional_skills"])


# --- v1 ----------------------------------------------------------------------

def _v1_entry(importance, weight, contribution, match_type="EXACT"):
    return {"importance": importance, "effective_jd_weight": weight, "skill_contribution": contribution,
            "match_type": match_type}


def _v1_provisional(tier, found, weight=1.0):
    return {"provisional": True, "tier": tier, "mandatory": tier != "PREFERRED",
            "importance": tier.lower() if tier in ("CORE", "SUPPORTING") else None,
            "effective_jd_weight": weight, "skill_contribution": round(weight * 0.8, 4) if found else 0.0,
            "match_type": "PROVISIONAL_TEXT" if found else "MISSING"}
