"""
No counted experience is 0 years, not "not applicable" (v1 and v2). With a
1-year minimum and the default 1-year near-miss tolerance, a fresher with
no roles, or only internships, is a near-miss (HOLD) - it used to pass the
experience check outright. A professional role whose dates can't be read
is still "data missing", never held against the candidate.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.services.campaign.experience_education_validation_service import ExperienceEducationValidationService
from app.services.campaign.near_miss import evaluate_near_miss
from app.services.campaign.scoring_v2 import (
    EvidenceInput, JDSkillInput, ScoringInputs, merge_config, score_v2,
)
from app.services.jd.jd_calibration_rules import apply_experience_tolerance
from app.services.resume.work_experience_duration import experience_years_for_scoring

INTERN_A = {"title": "Software Engineering Intern", "start_date": "Jan 2026", "end_date": "Jun 2026",
            "is_internship": True}
INTERN_B = {"title": "Backend Developer Intern", "start_date": "Jul 2025", "end_date": "Dec 2025",
            "is_internship": True}
JOB = {"title": "Developer", "start_date": "Jan 2020", "end_date": "Jan 2022"}   # 24 months
JOB_NO_DATES = {"title": "Developer", "start_date": None, "end_date": None}


# --- what counts --------------------------------------------------------------

@pytest.mark.parametrize("parsed, expected", [
    ({"work_experience": []}, 0.0),                                    # fresher, projects only
    ({}, 0.0),
    ({"work_experience": [INTERN_A, INTERN_B]}, 0.0),                  # internships only
    ({"work_experience": [{**JOB, "is_volunteer": True}]}, 0.0),       # volunteer only
    ({"work_experience": [JOB]}, 2.0),                                 # professional, dated
    ({"work_experience": [JOB, INTERN_A]}, 2.0),                       # internship still excluded
    ({"work_experience": [], "total_experience_years": 3}, 3.0),       # no roles, but the resume states years
])
def test_counted_experience(parsed, expected):
    assert experience_years_for_scoring(parsed) == expected


def test_a_professional_role_with_unreadable_dates_is_still_data_missing():
    assert experience_years_for_scoring({"work_experience": [JOB_NO_DATES]}) is None
    assert experience_years_for_scoring({"work_experience": [JOB_NO_DATES, INTERN_A]}) is None


# --- v1: the experience check and the near-miss rule ---------------------------

def _jd(min_years=1.0):
    return SimpleNamespace(experience_tolerance_years=None, calibrated_min_experience_years=min_years,
                           calibration_status=SimpleNamespace(value="CALIBRATED"))


def _experience(parsed, min_years=1.0):
    result = ExperienceEducationValidationService().validate_experience(
        min_years, experience_years_for_scoring(parsed))
    return apply_experience_tolerance(result, _jd(min_years))


def test_v1_a_fresher_is_short_not_skipped():
    result = _experience({"work_experience": [INTERN_A, INTERN_B]})
    assert result["applicable"] is True and result["data_missing"] is False
    assert result["candidate_years"] == 0.0 and result["passed"] is False


def test_v1_zero_years_against_a_1_year_minimum_is_a_near_miss_hold():
    decision = evaluate_near_miss(core_skill_count=4, confirmed_core_count=4, allowed_missing_core=1,
                                  definite_missing_core=0, experience=_experience({"work_experience": []}))
    assert decision.needs_review is True and decision.outcome == "HOLD"


def test_v1_zero_years_against_a_4_year_minimum_is_rejected():
    decision = evaluate_near_miss(core_skill_count=4, confirmed_core_count=4, allowed_missing_core=1,
                                  definite_missing_core=0,
                                  experience=_experience({"work_experience": []}, min_years=4.0))
    assert decision.needs_review is False and decision.outcome == "REJECT"


def test_v1_unreadable_dates_still_pass_as_data_missing():
    result = _experience({"work_experience": [JOB_NO_DATES]})
    assert result["applicable"] is False and result["data_missing"] is True and result["passed"] is True


# --- v2 --------------------------------------------------------------------------

def test_v2_a_fresher_with_every_core_skill_is_a_near_miss_hold():
    core = [uuid4() for _ in range(4)]
    inputs = ScoringInputs(
        jd_skills=[JDSkillInput(s, f"Skill {i}", True, 1.0, "core") for i, s in enumerate(core)],
        evidence=[EvidenceInput(s, None, ("SCAN",), "SCAN", "SCAN", 1.0, "line") for s in core],
        confirmations={str(s): {"status": "CONFIRMED"} for s in core},
        hierarchy={"skills_by_id": {}, "children_by_parent": {}, "grandchildren_by_parent": {}},
        experience_result=_experience({"work_experience": [INTERN_A, INTERN_B]}),
        deterministic_threshold=40.0, allowed_missing_core=1,
    )
    result = score_v2(inputs, merge_config(None))
    experience_check = next(c for c in result["checks"] if c["check"] == "Experience")
    assert experience_check["applies"] is True and experience_check["passed"] is False
    assert result["outcome"] == "REVIEW"            # shown as HOLD
    assert result["would_be_review"] is True


def test_the_scoring_inputs_use_counted_experience(monkeypatch):
    """build_validation_inputs (v1 task and v2) reads the counted years, not the raw total."""
    from app.tasks import deterministic_scoring_tasks as tasks
    config = MagicMock()
    config.get_configs_by_keys.return_value = {}
    jd = SimpleNamespace(min_experience_years=1, extracted_json={}, education_criteria={},
                         experience_tolerance_years=None, calibrated_min_experience_years=1.0,
                         calibration_status=SimpleNamespace(value="CALIBRATED"),
                         education_requirement=SimpleNamespace(value="MANDATORY"))
    resume = SimpleNamespace(parsed_json={"work_experience": [INTERN_A, INTERN_B], "education": []})
    experience, *_ = tasks.build_validation_inputs(config, jd, resume)
    assert experience["candidate_years"] == 0.0 and experience["passed"] is False
