"""Scoring v2: formula A/B/C, confirmation statuses, gates, would_be_review, config versioning, task wiring."""
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.services.campaign.scoring_v2 import (
    DEFAULT_CONFIG, EvidenceInput, JDSkillInput, ScoringInputs, config_version, merge_config, parse_config,
    score_v2,
)

CORE_A, CORE_B, SUP_C, SUP_D, CHILD_OF_A, PREF_E = (uuid4() for _ in range(6))


def _skill(skill_id, name, importance="supporting", mandatory=True, sense=None, basis=None, weight=1.0):
    return JDSkillInput(skill_id, name, mandatory, weight, importance if mandatory else None,
                        sense=sense, sense_basis=basis)


def _ev(skill_id, source="SCAN", tier="SCAN", sense=None):
    return EvidenceInput(skill_id, sense, (source,), source, tier, 1.0, "line")


JD = [
    _skill(CORE_A, "Program Management", "core"),
    _skill(CORE_B, "AI Transformation", "core"),
    _skill(SUP_C, "Stakeholder Management"),
    _skill(SUP_D, "Risk Management"),
]
NO_HIERARCHY = {"skills_by_id": {}, "children_by_parent": {}, "grandchildren_by_parent": {}}


def _confirm(**statuses):
    ids = {"A": CORE_A, "B": CORE_B}
    return {str(ids[k]): {"status": v} for k, v in statuses.items()}


def _score(evidence, confirmations=None, *, jd=JD, hierarchy=NO_HIERARCHY, config=None, **kw):
    inputs = ScoringInputs(jd_skills=jd, evidence=evidence, confirmations=confirmations or {}, hierarchy=hierarchy,
                           deterministic_threshold=kw.pop("threshold", 40.0), allowed_missing_core=kw.pop("allowed", 0),
                           **kw)
    return score_v2(inputs, merge_config(config))


def _entry(result, name):
    return next(e for e in result["mandatory_skills"] if e["canonical_name"] == name)


# --- A: core weighted x2 in the score ------------------------------------

def test_core_skills_are_scored_at_double_weight():
    result = _score([_ev(CORE_A), _ev(SUP_C)], _confirm(A="CONFIRMED", B="NOT_FOUND"), allowed=1)
    # weights: core 2+2, supporting 1+1 = 6; matched: core A (2) + supporting C (1) = 3 -> 50
    assert result["required_skill_score"] == 50.0
    assert _entry(result, "Program Management")["effective_weight"] == 2.0


# --- B: experience / education are gates, not averaged -------------------

def test_experience_and_education_are_gates_only():
    passing = {"applicable": True, "passed": True, "score": 0.0, "candidate_years": 12, "min_years": 10}
    result = _score([_ev(CORE_A), _ev(CORE_B)], _confirm(A="CONFIRMED", B="CONFIRMED"),
                    experience_result=passing, education_result={"applicable": True, "passed": True, "score": 0.0})
    assert [c["name"] for c in result["components"]] == ["skills"]      # a 0 experience score didn't lower it
    failing = {**passing, "passed": False, "candidate_years": 8.6}
    rejected = _score([_ev(CORE_A), _ev(CORE_B)], _confirm(A="CONFIRMED", B="CONFIRMED"), experience_result=failing)
    assert rejected["deterministic_passed"] is False and "Experience" in rejected["failed_checks"]


def test_functional_score_is_blended_when_applicable():
    result = _score([_ev(CORE_A), _ev(CORE_B), _ev(SUP_C), _ev(SUP_D)], _confirm(A="CONFIRMED", B="CONFIRMED"),
                    domain_result={"applicable": True, "score": 0.0})
    assert result["skill_score"] == 100.0
    assert result["deterministic_score"] == round(100 * 0.70 / 0.85, 2)


# --- C: multipliers / weights from config --------------------------------

def test_configured_multipliers_and_weights_are_used():
    hierarchy = {
        "skills_by_id": {CORE_A: SimpleNamespace(id=CORE_A, canonical_name="Program Management", parent_skill_id=None, is_active=True)},
        "children_by_parent": {CORE_A: [SimpleNamespace(id=CHILD_OF_A, canonical_name="PgM Child", parent_skill_id=CORE_A, is_active=True)]},
        "grandchildren_by_parent": {},
    }
    default = _score([_ev(CHILD_OF_A)], jd=[_skill(CORE_A, "Program Management")], hierarchy=hierarchy)
    tuned = _score([_ev(CHILD_OF_A)], jd=[_skill(CORE_A, "Program Management")], hierarchy=hierarchy,
                   config={"hierarchy_multipliers": {"CHILD": 0.9}})
    assert default["required_skill_score"] == 70.0 and tuned["required_skill_score"] == 90.0


def test_config_version_changes_with_config_and_defaults_are_todays_values():
    assert DEFAULT_CONFIG["hierarchy_multipliers"] == {"EXACT": 1.0, "CHILD": 0.7, "GRANDCHILD": 0.5, "SIBLING": 0.4, "SEMANTIC": 0.2}
    assert DEFAULT_CONFIG["importance_weights"]["core"] == 2.0
    assert parse_config("not json") == merge_config(None)
    assert config_version({"c": merge_config(None)}) != config_version({"c": merge_config({"preferred_share": 0.2})})


# --- confirmation statuses -----------------------------------------------

def test_only_confirmed_core_counts_for_the_gate_unconfirmed_still_scores():
    result = _score([_ev(CORE_A), _ev(CORE_B)], _confirm(A="CONFIRMED", B="UNCONFIRMED"), allowed=0)
    assert result["core_confirmed"] == ["Program Management"]
    assert result["core_found_not_confirmed"] == ["AI Transformation"]
    assert _entry(result, "AI Transformation")["contribution"] == 2.0      # supporting-evidence credit kept
    assert result["failed_checks"] == ["Core skills"]                       # 1 missing > 0 allowed
    assert result["outcome"] == "REVIEW"                                    # ...by one: a near-miss


def test_rejected_core_match_is_discarded_entirely():
    result = _score([_ev(CORE_A), _ev(CORE_B)], _confirm(A="CONFIRMED", B="REJECTED"), allowed=1)
    entry = _entry(result, "AI Transformation")
    assert (entry["match_type"], entry["contribution"], entry["counts_for_core_gate"]) == ("MISSING", 0.0, False)
    assert result["core_rejected_by_llm"] == ["AI Transformation"]


def test_rejected_evidence_cannot_serve_as_a_hierarchy_match():
    # Supporting skill D's child is core skill B's evidence; B rejected -> D gets nothing from it.
    hierarchy = {"skills_by_id": {}, "grandchildren_by_parent": {},
                 "children_by_parent": {SUP_D: [SimpleNamespace(id=CORE_B, canonical_name="AI Transformation",
                                                                parent_skill_id=SUP_D, is_active=True)]}}
    result = _score([_ev(CORE_B)], _confirm(B="REJECTED"), hierarchy=hierarchy, allowed=2)
    assert _entry(result, "Risk Management")["match_type"] == "MISSING"


def test_not_run_confirmation_is_supporting_evidence_only():
    result = _score([_ev(CORE_A)], {}, allowed=2)
    assert _entry(result, "Program Management")["confirmation_status"] == "NOT_RUN"
    assert _entry(result, "Program Management")["counts_for_core_gate"] is False
    assert result["core_found_not_confirmed"] == ["Program Management"]


def test_definite_sense_mismatch_gets_at_most_sibling_credit():
    jd = [_skill(CORE_A, "Change Management", "core", sense="ORGANISATIONAL", basis="cue")]
    result = _score([_ev(CORE_A, sense="ITSM")], {}, jd=jd, allowed=1)
    entry = _entry(result, "Change Management")
    assert (entry["match_type"], entry["quality"], entry["counts_for_core_gate"]) == ("SENSE_MISMATCH", 0.4, False)
    assert result["core_sense_mismatch"] == ["Change Management"]


def test_assumed_jd_sense_is_not_a_definite_mismatch():
    jd = [_skill(CORE_A, "Change Management", "core", sense="ORGANISATIONAL", basis="assumed")]
    result = _score([_ev(CORE_A, sense="ITSM")], {str(CORE_A): {"status": "CONFIRMED"}}, jd=jd)
    assert _entry(result, "Change Management")["counts_for_core_gate"] is True


def test_core_matched_only_through_hierarchy_scores_but_does_not_gate():
    hierarchy = {"skills_by_id": {}, "grandchildren_by_parent": {},
                 "children_by_parent": {CORE_A: [SimpleNamespace(id=CHILD_OF_A, canonical_name="Child", parent_skill_id=CORE_A, is_active=True)]}}
    result = _score([_ev(CHILD_OF_A)], {}, jd=[_skill(CORE_A, "Program Management", "core")], hierarchy=hierarchy, allowed=0)
    entry = _entry(result, "Program Management")
    assert (entry["match_type"], entry["counts_for_core_gate"]) == ("CHILD", False)
    assert result["core_via_hierarchy_only"] == ["Program Management"]


def test_weak_llm_match_uses_configured_recognition():
    result = _score([_ev(SUP_C, source="LLM", tier="FUZZY")], jd=[_skill(SUP_C, "Stakeholder Management")])
    assert _entry(result, "Stakeholder Management")["recognition"] == 0.8


def test_preferred_share_is_ten_percent():
    jd = JD + [_skill(PREF_E, "Kubernetes", mandatory=False)]
    result = _score([_ev(CORE_A), _ev(CORE_B), _ev(SUP_C), _ev(SUP_D)], _confirm(A="CONFIRMED", B="CONFIRMED"), jd=jd)
    assert result["skill_score"] == 90.0


# --- gates + would_be_review ---------------------------------------------

def test_score_threshold_does_not_reject_when_core_skills_exist():
    result = _score([_ev(CORE_A), _ev(CORE_B)], _confirm(A="CONFIRMED", B="CONFIRMED"), threshold=99)
    assert result["deterministic_passed"] is True and result["score_threshold_rejects"] is False


def test_one_near_miss_is_review_and_goes_on_capped_at_hold():
    one_short = _score([_ev(CORE_A)], _confirm(A="CONFIRMED", B="NOT_FOUND"), allowed=0)
    assert one_short["outcome"] == "REVIEW" and one_short["would_be_review"] is True
    assert one_short["deterministic_passed"] is True and one_short["needs_review"] is True
    assert one_short["needs_review_reason"].startswith("Needs recruiter review: 1 core skill over")
    far = _score([], _confirm(A="NOT_FOUND", B="NOT_FOUND"), allowed=0)
    assert far["outcome"] == "REJECT" and far["would_be_review"] is False    # no confirmed core skill
    exp = {"applicable": True, "passed": False, "candidate_years": 8.6, "min_years": 10}
    both = _score([_ev(CORE_A), _ev(CORE_B)], _confirm(A="CONFIRMED", B="CONFIRMED"), experience_result=exp)
    assert both["outcome"] == "REJECT"                                        # 1.4 years short > 1.0 tolerance


# --- task wiring ----------------------------------------------------------

def test_v2_runs_only_on_opted_in_campaigns():
    from app.services.campaign.scoring_v2_service import v2_should_run
    assert v2_should_run(SimpleNamespace(scoring_v2_enabled=False, scoring_v2_shadow=False)) is False
    assert v2_should_run(SimpleNamespace(scoring_v2_enabled=False, scoring_v2_shadow=True)) is True
    assert v2_should_run(SimpleNamespace(scoring_v2_enabled=True, scoring_v2_shadow=False)) is True
    assert v2_should_run(MagicMock()) is False            # truthy mocks never opt a campaign in
    assert v2_should_run(SimpleNamespace()) is False


def test_v2_failure_returns_none_and_never_raises(monkeypatch):
    import app.tasks.deterministic_scoring_tasks as task
    monkeypatch.setattr(task, "v2_should_run", lambda *a: True)
    monkeypatch.setattr(task, "ScoringV2Service", MagicMock(side_effect=RuntimeError("boom")))
    assert task._run_scoring_v2(MagicMock(), MagicMock(), MagicMock(), SimpleNamespace(), SimpleNamespace(id=uuid4()),
                                None, None, None, None, None, acted_on=False) is None


def test_acting_on_v2_keeps_v1_numbers_beside_it():
    import app.tasks.deterministic_scoring_tasks as task
    cc = SimpleNamespace(deterministic_breakdown=None, deterministic_score=None, deterministic_passed=None)
    acted = task._act_on_v2(cc, {"deterministic_score": 22.5, "deterministic_passed": True, "mandatory_skills": []},
                            {"deterministic_score": 41.8, "deterministic_passed": False})
    assert (cc.deterministic_score, cc.deterministic_passed) == (41.8, False)
    assert (acted["v1_deterministic_score"], acted["v1_deterministic_passed"], acted["acted_on_version"]) == (22.5, True, "v2")


@pytest.mark.parametrize("passed", [True, False])
def test_v1_result_row_records_snapshot(passed):
    from app.services.campaign.scoring_v2_service import v1_result_row
    campaign = SimpleNamespace(deterministic_threshold=40, required_skill_coverage_threshold=0)
    row = v1_result_row(campaign, SimpleNamespace(id=uuid4()),
                        {"deterministic_score": 30.0, "deterministic_passed": passed, "allowed_missing_core_skills": 1},
                        acted_on=True, score_weights={"skills": 0.7})
    assert row.scoring_version == "v1" and row.acted_on is True and row.outcome == ("PASS" if passed else "REJECT")
    assert row.config_snapshot["score_weights"] == {"skills": 0.7} and len(row.scoring_config_version) == 16


def test_uncertain_core_is_supporting_at_0_8_and_flags_review():
    result = _score([_ev(CORE_A), _ev(CORE_B)], _confirm(A="CONFIRMED", B="UNCERTAIN"), allowed=0)
    entry = _entry(result, "AI Transformation")
    assert (entry["recognition"], entry["counts_for_core_gate"]) == (0.8, False)
    assert result["core_uncertain"] == ["AI Transformation"]
    assert result["outcome"] == "REVIEW" and result["would_be_review"] is True


def test_v2_decides_only_on_enabled_campaigns():
    from app.tasks.deterministic_scoring_tasks import _v2_decides
    assert _v2_decides(SimpleNamespace(scoring_v2_enabled=True, scoring_v2_shadow=True)) is True
    assert _v2_decides(SimpleNamespace(scoring_v2_enabled=False, scoring_v2_shadow=True)) is False   # shadow only
    assert _v2_decides(SimpleNamespace()) is False
    assert _v2_decides(MagicMock()) is False             # a truthy mock never opts a campaign in
