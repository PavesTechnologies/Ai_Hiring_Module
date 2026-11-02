"""
Retire v1, phase B - the no-LLM fallback's decision (user rule, 2026-11-02): REJECT only on a
rule-only failure (experience short by more than the tolerance, mandatory education missing);
missing or unconfirmed core skills are UNCERTAIN, so REVIEW (HOLD), marked awaiting full scoring;
every core skill auto-confirmed - the full outcome.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.campaign.scoring_v2 import (
    OUTCOME_PASS, OUTCOME_REJECT, OUTCOME_REVIEW, SCORING_MODE_NO_LLM, apply_no_llm_outcome,
)


def _core(name, confirmed):
    return {"canonical_name": name, "importance": "core", "counts_for_core_gate": confirmed}


def _breakdown(*core, outcome=OUTCOME_REJECT, passed=False):
    return {"outcome": outcome, "deterministic_passed": passed, "mandatory_skills": list(core),
            "needs_review": False, "core_uncertain": []}


def _experience(years, minimum=4.0, tolerance=None):
    return {"applicable": True, "passed": years >= minimum, "candidate_years": years, "min_years": minimum,
            "near_miss_tolerance_years": tolerance}


EDUCATION_OK = {"applicable": True, "passed": True}
EDUCATION_MISSING = {"applicable": True, "passed": False}


def test_unconfirmed_core_skills_hold_for_full_scoring_never_reject():
    b = apply_no_llm_outcome(_breakdown(_core("Python", False), _core("SQL", True)), _experience(6), EDUCATION_OK,
                             "daily LLM call cap reached")
    assert b["outcome"] == OUTCOME_REVIEW and b["deterministic_passed"] is True and b["needs_review"] is True
    assert b["awaiting_full_scoring"] is True and b["scoring_mode"] == SCORING_MODE_NO_LLM
    assert b["needs_review_reason"].startswith("Awaiting full scoring (daily LLM call cap reached)")
    assert "Python" in b["needs_review_reason"] and b["core_uncertain"] == ["Python"]


def test_no_core_skill_confirmed_is_still_a_hold_not_a_reject():
    # Full scoring rejects "No core skill confirmed"; without the votes nobody can say that.
    b = apply_no_llm_outcome(_breakdown(_core("Python", False), _core("SQL", False)), _experience(6), EDUCATION_OK,
                             "LLM unavailable")
    assert b["outcome"] == OUTCOME_REVIEW and b["awaiting_full_scoring"] is True


@pytest.mark.parametrize("experience, education, rejected_for", [
    (_experience(2.0), EDUCATION_OK, "Experience"),          # 2.0 short of 4: more than the 1-year tolerance
    (_experience(6), EDUCATION_MISSING, "Education"),
])
def test_a_rule_only_failure_rejects(experience, education, rejected_for):
    b = apply_no_llm_outcome(_breakdown(_core("Python", False)), experience, education, "daily LLM call cap reached")
    assert b["outcome"] == OUTCOME_REJECT and b["deterministic_passed"] is False
    assert b["awaiting_full_scoring"] is False and b["needs_review"] is False
    assert any(rejected_for in r for r in b["near_miss_reject_reasons"])


def test_experience_within_tolerance_with_unconfirmed_core_is_a_hold():
    # Full scoring would count two near-misses and reject; the fallback never rejects on core skills.
    b = apply_no_llm_outcome(_breakdown(_core("Python", False)), _experience(3.4), EDUCATION_OK, "LLM unavailable")
    assert b["outcome"] == OUTCOME_REVIEW and b["awaiting_full_scoring"] is True
    assert any("experience 3.4 years" in n for n in b["near_misses"])


def test_every_core_skill_auto_confirmed_gives_the_full_outcome():
    b = apply_no_llm_outcome(_breakdown(_core("Python", True), outcome=OUTCOME_PASS, passed=True), _experience(6),
                             EDUCATION_OK, "daily LLM call cap reached")
    assert b["outcome"] == OUTCOME_PASS and b["awaiting_full_scoring"] is False
    assert b["scoring_mode"] == SCORING_MODE_NO_LLM and b["fallback_reason"] == "daily LLM call cap reached"


def test_the_service_turns_every_unsettled_core_skill_uncertain_without_a_call():
    from app.services.campaign import scoring_v2_service as module
    outcome = SimpleNamespace(results=[
        {"canonical_skill_id": "a", "status": "CONFIRMED", "reason": "auto_exact_scan"},
        {"canonical_skill_id": "b", "status": "UNCONFIRMED", "reason": "daily_llm_cap"},
        {"canonical_skill_id": "c", "status": "NOT_FOUND", "reason": "not_found"},
    ])
    service = module.ScoringV2Service.__new__(module.ScoringV2Service)
    confirm_service = MagicMock()
    confirm_service.return_value.confirm.return_value = outcome
    with patch.object(module, "CoreSkillConfirmationService", confirm_service):
        results = service._no_llm_confirmations(
            [SimpleNamespace()], [], "h", (), SimpleNamespace(rules=None), None,
            SimpleNamespace(id="jd", calibration_profile=None, profile_template_key=None), None)
    provider, repository = confirm_service.call_args.args
    assert provider is None and repository is None                       # nothing to call, nothing cached
    kwargs = confirm_service.return_value.confirm.call_args.kwargs
    assert kwargs["budget"](1) is False and kwargs["use_cache"] is False and kwargs["persist"] is False
    assert {k: v["status"] for k, v in results.items()} == {"a": "CONFIRMED", "b": "UNCERTAIN", "c": "UNCERTAIN"}
    assert results["b"]["reason"] == results["c"]["reason"] == "awaiting_full_scoring"
