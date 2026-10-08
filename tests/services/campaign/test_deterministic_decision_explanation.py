from app.services.campaign.candidate_scoring_service import CandidateScoringService

"""
The deterministic tab must explain every decision - including why a
candidate below the score threshold passed under the core-skill rule - and
a rejection reason must name only what actually caused the rejection.
"""


def _core(name, missing):
    return {"canonical_name": name, "canonical_skill_id": name, "importance": "core",
            "match_type": "MISSING" if missing else "EXACT"}


def _supporting(name, missing=True):
    return {"canonical_name": name, "canonical_skill_id": name, "importance": "supporting",
            "match_type": "MISSING" if missing else "EXACT"}


def _breakdown(**overrides):
    b = {
        "mandatory_skills": [
            _core("AI Transformation", True), _core("AI Governance", False),
            _core("Program Management", False), _core("Change Management", False),
            _supporting("Risk Management"), _supporting("Responsible AI"),
        ],
        "gate_skill_scope": "CORE", "core_gap_passed": True, "missing_core_skill_count": 1,
        "max_missing_core_skills": 3, "coverage_passed": True, "required_skill_coverage_threshold": 0.0,
        "core_coverage_pct": 75.0, "mandatory_coverage_pct": 50.0,
        "deterministic_score": 22.5, "deterministic_threshold": 40.0, "score_threshold_rejects": False,
        "experience_validation": {"applicable": True, "passed": True, "candidate_years": 24.2, "min_years": 10.0},
    }
    b.update(overrides)
    return b


def test_pass_below_threshold_is_explained():
    checks = {c["check"]: c for c in CandidateScoringService.explain_decision(_breakdown())}

    assert checks["Core skills"]["passed"] is True
    assert "3 of 4 core skills matched" in checks["Core skills"]["detail"]
    score = checks["Score threshold"]
    assert (score["passed"], score["can_reject"]) == (False, False)
    assert "does not reject" in score["detail"]
    assert checks["Experience"]["passed"] is True
    assert "Core skill coverage" not in checks          # a 0% coverage threshold isn't a real check


def test_score_rule_on_says_it_can_reject():
    checks = {c["check"]: c for c in CandidateScoringService.explain_decision(_breakdown(score_threshold_rejects=True))}
    assert checks["Score threshold"]["can_reject"] is True
    assert "does not reject" not in checks["Score threshold"]["detail"]


def test_rejection_reason_names_only_missing_core_skills_under_core_rule():
    reason = CandidateScoringService.build_rejection_reason(_breakdown(
        mandatory_skills=[_core("AI Transformation", True), _core("Program Management", True),
                          _core("AI Governance", True), _core("Change Management", True),
                          _supporting("Risk Management")],
        core_gap_passed=False, missing_core_skill_count=4,
    ))
    assert reason == ("Missing core required skills: 4 exceeds the maximum allowed (3) - "
                      "AI Transformation, Program Management, AI Governance, Change Management.")
    assert "Risk Management" not in reason


def test_experience_only_rejection_does_not_list_skills():
    reason = CandidateScoringService.build_rejection_reason(
        _breakdown(), experience_result={"passed": False, "candidate_years": 8.6, "min_years": 10.0},
    )
    assert reason.startswith("Insufficient experience")
    assert "Missing" not in reason


def test_legacy_score_rule_keeps_listing_every_missing_skill():
    reason = CandidateScoringService.build_rejection_reason(_breakdown(score_threshold_rejects=True))
    assert "Missing required skills: AI Transformation, Risk Management, Responsible AI" in reason


# ---------------------------------------------------------------- proportional missing-core limit

import pytest  # noqa: E402

from app.services.campaign.candidate_scoring_service import allowed_missing_core  # noqa: E402


@pytest.mark.parametrize("core,pct,cap,expected", [
    (4, 25, 3, 1), (3, 25, 3, 0), (8, 25, 3, 2), (12, 25, 3, 3),
    (20, 25, 3, 3),     # percentage says 5 - the fixed max caps it
    (4, 50, 3, 2), (1, 25, 3, 0), (0, 25, 3, 0), (10, 0, 3, 0),
    (4, None, 3, 3),    # no percentage: fixed count on its own (pre-percentage behaviour)
])
def test_allowed_missing_is_percentage_capped_by_max(core, pct, cap, expected):
    assert allowed_missing_core(core, cap, pct) == expected


def _core_breakdown(missing):
    return {"mandatory_skills": [
        {"canonical_name": f"S{i}", "importance": "core", "match_type": "MISSING" if i < missing else "EXACT"}
        for i in range(4)
    ], "core_coverage_pct": 100.0, "mandatory_coverage_pct": 100.0}


def test_four_core_skills_at_25_percent_allow_one_missing():
    service = CandidateScoringService(None, None, None, None)

    one = service.evaluate_skill_qualification(_core_breakdown(1), 90.0, 40.0, 0.0, 3, 25.0)
    two = service.evaluate_skill_qualification(_core_breakdown(2), 90.0, 40.0, 0.0, 3, 25.0)

    assert (one["allowed_missing_core_skills"], one["core_gap_passed"]) == (1, True)
    assert (two["allowed_missing_core_skills"], two["core_gap_passed"]) == (1, False)


def test_explanation_shows_the_percentage_rule():
    checks = CandidateScoringService.explain_decision(_breakdown(
        missing_core_skill_count=1, allowed_missing_core_skills=1, max_missing_core_pct=25.0, max_missing_core_skills=3,
    ))
    core = next(c for c in checks if c["check"] == "Core skills")
    assert "up to 1 may be missing (25% of 4, max 3)" in core["detail"]
