"""
How JD calibration settings reach scoring: the per-JD near-miss tolerance,
education mandatory vs preferred (v1 gate / v2 component), and the HR-confirmed
sense of an ambiguous skill.
"""
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from app.models.jd.job_descriptions import JDEducationRequirement
from app.services.campaign.near_miss import OUTCOME_HOLD, OUTCOME_REJECT, evaluate_near_miss
from app.services.campaign.scoring_v2 import (
    EvidenceInput, JDSkillInput, ScoringInputs, merge_config, score_v2,
)
from app.services.jd.jd_calibration_rules import (
    apply_education_requirement, apply_experience_tolerance, jd_calibration_response_fields,
)
from app.services.skills.skill_sense_resolver import SenseDef, SkillSenseResolver

CORE = uuid4()


def _experience(years, min_years=10.0):
    return {"applicable": True, "passed": years >= min_years, "candidate_years": years, "min_years": min_years,
            "score": 80.0}


# --- experience tolerance --------------------------------------------------

def test_the_jd_tolerance_replaces_the_default_near_miss_band():
    jd = SimpleNamespace(experience_tolerance_years=Decimal("2.0"))
    experience = apply_experience_tolerance(_experience(8.0), jd)          # 2 years short
    assert experience["near_miss_tolerance_years"] == 2.0
    decision = evaluate_near_miss(core_skill_count=0, confirmed_core_count=0, allowed_missing_core=0,
                                  definite_missing_core=0, experience=experience)
    assert decision.outcome == OUTCOME_HOLD


def test_without_a_jd_tolerance_the_default_applies():
    jd = SimpleNamespace(experience_tolerance_years=None)
    experience = apply_experience_tolerance(_experience(8.0), jd)          # default max(1, 10%) = 1 year
    assert experience["near_miss_tolerance_years"] is None
    decision = evaluate_near_miss(core_skill_count=0, confirmed_core_count=0, allowed_missing_core=0,
                                  definite_missing_core=0, experience=experience)
    assert decision.outcome == OUTCOME_REJECT


# --- education ---------------------------------------------------------------

def test_preferred_education_is_never_a_gate_but_keeps_the_real_outcome():
    jd = SimpleNamespace(education_requirement=JDEducationRequirement.PREFERRED)
    result = apply_education_requirement({"applicable": True, "passed": False, "score": 40.0}, jd)
    assert result["passed"] is True and result["requirement_met"] is False and result["requirement"] == "PREFERRED"


def test_mandatory_education_is_unchanged():
    jd = SimpleNamespace(education_requirement=JDEducationRequirement.MANDATORY)
    result = apply_education_requirement({"applicable": True, "passed": False, "score": 40.0}, jd)
    assert result["passed"] is False and result["requirement"] == "MANDATORY"


def _v2(education):
    jd_skills = [JDSkillInput(CORE, "Program Management", True, 1.0, "core")]
    evidence = [EvidenceInput(CORE, None, ("SCAN",), "SCAN", "SCAN", 1.0, "line")]
    inputs = ScoringInputs(jd_skills=jd_skills, evidence=evidence,
                           confirmations={str(CORE): {"status": "CONFIRMED"}},
                           hierarchy={"skills_by_id": {}, "children_by_parent": {}, "grandchildren_by_parent": {}},
                           deterministic_threshold=40.0, allowed_missing_core=0, education_result=education)
    return score_v2(inputs, merge_config(None))


def test_v2_counts_preferred_education_as_a_small_component():
    jd = SimpleNamespace(education_requirement=JDEducationRequirement.PREFERRED)
    preferred = apply_education_requirement({"applicable": True, "passed": False, "score": 0.0}, jd)
    result = _v2(preferred)
    components = {c["name"]: c for c in result["components"]}
    assert components["education_preferred"]["weight"] == 0.05
    assert result["deterministic_passed"] is True                          # never a gate
    assert result["deterministic_score"] < 100.0                           # but it lowers the score a little


def test_v2_mandatory_education_stays_gate_only():
    jd = SimpleNamespace(education_requirement=JDEducationRequirement.MANDATORY)
    mandatory = apply_education_requirement({"applicable": True, "passed": False, "score": 0.0}, jd)
    result = _v2(mandatory)
    assert "education_preferred" not in {c["name"] for c in result["components"]}
    assert "Education" in result["failed_checks"]


def test_the_education_component_weight_is_configurable():
    from app.services.campaign.scoring_v2 import DEFAULT_CONFIG
    assert merge_config({"component_weights": {"education_preferred": 0.1}})["component_weights"][
        "education_preferred"] == 0.1
    assert DEFAULT_CONFIG["component_weights"]["education_preferred"] == 0.05


# --- senses ----------------------------------------------------------------------

def _resolver():
    return SkillSenseResolver([
        SenseDef(CORE, "ITSM", "IT change control", "CAB", ("cab",)),
        SenseDef(CORE, "ORGANISATIONAL", "People side", "adoption", ("adoption",), is_default=True),
    ])


def test_the_hr_confirmed_sense_wins_over_cues_and_is_definite():
    choice = _resolver().resolve_jd_sense(CORE, "Run the CAB.", "Change Management", hr_sense="ORGANISATIONAL")
    assert (choice.sense, choice.basis) == ("ORGANISATIONAL", "hr")


def test_an_unknown_hr_sense_is_ignored():
    choice = _resolver().resolve_jd_sense(CORE, "No cues here.", "Change Management", hr_sense="BOGUS")
    assert (choice.sense, choice.basis) == ("ORGANISATIONAL", "assumed")


def test_an_hr_sense_counts_as_a_definite_mismatch_in_v2_and_confirmation():
    from app.services.campaign.scoring_v2 import _DEFINITE_JD_SENSE
    from app.services.skills.core_skill_confirmation_service import _DEFINITE_JD_SENSE as CONFIRM_DEFINITE
    assert "hr" in _DEFINITE_JD_SENSE and "hr" in CONFIRM_DEFINITE


# --- response fields ---------------------------------------------------------------

def test_response_fields_are_safe_for_bare_objects():
    assert jd_calibration_response_fields(SimpleNamespace()) == {
        "calibration_status": "NOT_CALIBRATED", "calibration_version": 0, "calibrated_at": None}
