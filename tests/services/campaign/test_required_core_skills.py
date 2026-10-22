"""Required core skills (hard negatives H8): never covered by the missing-core allowance, v1 and v2."""
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.services.campaign.candidate_scoring_service import CandidateScoringService
from app.services.campaign.scoring_v2 import (
    EvidenceInput, JDSkillInput, ScoringInputs, merge_config, required_core_allowance, score_v2,
)

PY, SQL, REST, GIT = (uuid4() for _ in range(4))
NO_HIERARCHY = {"skills_by_id": {}, "children_by_parent": {}, "grandchildren_by_parent": {}}
CONFIRMED = {"status": "CONFIRMED"}


def _jd(python_required=True):
    return [JDSkillInput(PY, "Python", True, 1.0, "core", required=python_required),
            JDSkillInput(SQL, "sql", True, 1.0, "core"),
            JDSkillInput(REST, "REST APIs", True, 1.0, "core"),
            JDSkillInput(GIT, "git", True, 1.0, "core")]


def _v2(present, *, python_required=True, experience=None):
    evidence = [EvidenceInput(s, None, ("SCAN",), "SCAN", "SCAN", 1.0, "line") for s in present]
    inputs = ScoringInputs(jd_skills=_jd(python_required), evidence=evidence,
                           confirmations={str(s): CONFIRMED for s in present}, hierarchy=NO_HIERARCHY,
                           allowed_missing_core=1, experience_result=experience)
    return score_v2(inputs, merge_config(None))


@pytest.mark.parametrize("allowed, missing, required, expected", [
    (1, 1, 1, 0),        # the one missing is required: nothing to cover
    (1, 1, 0, 1),
    (1, 2, 1, 1),        # one required + one not: the allowance covers the other one
    (2, 1, 0, 1),
    (0, 1, 0, 0),
])
def test_allowance_covers_only_non_required_missing(allowed, missing, required, expected):
    assert required_core_allowance(allowed, missing, required) == expected


def test_v2_java_developer_without_required_python_is_held_for_review_not_passed():
    result = _v2([SQL, REST, GIT])
    assert "Core skills" in result["failed_checks"] and result["outcome"] == "REVIEW"
    assert result["near_miss_outcome"] == "HOLD" and result["required_core_missing"] == ["Python"]
    assert result["configured_allowed_missing_core_skills"] == 1 and result["allowed_missing_core_skills"] == 0
    assert "Required, never allowed missing: Python." in next(c["detail"] for c in result["checks"]
                                                                if c["check"] == "Core skills")


def test_v2_without_the_flag_the_allowance_still_applies():
    result = _v2([SQL, REST, GIT], python_required=False)
    assert "Core skills" not in result["failed_checks"] and result["outcome"] == "PASS"


def test_v2_another_missing_core_skill_is_still_allowed_when_python_is_there():
    result = _v2([PY, SQL, REST])
    assert "Core skills" not in result["failed_checks"] and result["outcome"] == "PASS"


def test_v2_required_missing_plus_another_near_miss_rejects():
    experience = {"applicable": True, "passed": False, "candidate_years": 0.5, "min_years": 1.0}
    result = _v2([SQL, REST, GIT], experience=experience)
    assert result["near_miss_outcome"] == "REJECT" and result["outcome"] == "REJECT"


def _v1_entry(name, match_type, required=False):
    return {"canonical_name": name, "canonical_skill_id": str(uuid4()), "importance": "core",
            "match_type": match_type, "required": required}


def test_v1_required_missing_core_skill_is_over_the_limit_and_held():
    service = CandidateScoringService(MagicMock(), MagicMock(), MagicMock(), MagicMock())
    breakdown = {"core_coverage_pct": 75.0, "mandatory_coverage_pct": 75.0, "mandatory_skills": [
        _v1_entry("Python", "MISSING", required=True), _v1_entry("sql", "EXACT"),
        _v1_entry("REST APIs", "EXACT"), _v1_entry("git", "EXACT")]}
    qualification = service.evaluate_skill_qualification(breakdown, 80.0, 40.0, max_missing_core_skills=1)
    assert qualification["core_gap_passed"] is False and qualification["required_core_missing"] == ["Python"]
    assert qualification["allowed_missing_core_skills"] == 0
    assert qualification["configured_allowed_missing_core_skills"] == 1
    breakdown.update(qualification)
    decision = CandidateScoringService.evaluate_near_miss(breakdown, qualification, None, None, False, 80.0, 40.0, False)
    assert decision.outcome == "HOLD"
    explained = next(c for c in CandidateScoringService.explain_decision({**breakdown, "gate_skill_scope": "CORE"})
                     if c["check"] == "Core skills")
    assert "up to 1 may be missing" in explained["detail"] and "Required, never allowed missing: Python" in explained["detail"]


def test_v1_without_the_flag_unchanged():
    service = CandidateScoringService(MagicMock(), MagicMock(), MagicMock(), MagicMock())
    breakdown = {"core_coverage_pct": 75.0, "mandatory_coverage_pct": 75.0, "mandatory_skills": [
        _v1_entry("Python", "MISSING"), _v1_entry("sql", "EXACT"), _v1_entry("REST APIs", "EXACT"),
        _v1_entry("git", "EXACT")]}
    qualification = service.evaluate_skill_qualification(breakdown, 80.0, 40.0, max_missing_core_skills=1)
    assert qualification["core_gap_passed"] is True and qualification["required_core_missing"] == []
