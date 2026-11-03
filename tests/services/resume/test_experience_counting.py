"""
Phase 3 Step 1: how experience is counted for the minimum-experience check.

- Both end months count: "Jan 2026 - Jun 2026" is 6 months (it was 5).
- Concurrent roles count once (calendar months covered, not summed).
- "Present" (or is_current, or no end date) runs to the current month.
- Internships count as the JD says (100 / 50 / 0%), only for months not
  already covered by a professional role. Volunteer roles never count.
- Default: 100% on a fresher JD (calibrated minimum <= 1 year), else 0%.
"""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.jd.jd_calibration_rules import (
    default_internship_credit_pct, is_fresher_jd, jd_internship_credit, jd_internship_credit_pct,
)
from app.services.resume.work_experience_duration import (
    annotate_work_experience_durations, experience_years_for_scoring,
)


def _role(start, end, **flags):
    return {"title": "Role", "start_date": start, "end_date": end, **flags}


def _years(*roles, credit=0.0):
    return experience_years_for_scoring({"work_experience": list(roles)}, credit)


# Phase 3 F1: two 6-month internships, Jul 2025 - Dec 2025 and Jan 2026 - Jun 2026.
F1 = [_role("Jul 2025", "Dec 2025", is_internship=True), _role("Jan 2026", "Jun 2026", is_internship=True)]


def test_both_end_months_count():
    assert _years(_role("Jan 2026", "Jun 2026")) == 0.5
    assert _years(_role("Jan 2020", "Dec 2021")) == 2.0
    assert _years(_role("Mar 2024", "Mar 2024")) == 0.1          # one month


def test_concurrent_roles_are_not_counted_twice():
    full_time = _role("Jan 2020", "Dec 2021")
    side_job = _role("Jun 2020", "Dec 2020")                       # inside the full-time role
    assert _years(full_time, side_job) == 2.0
    overlapping = _role("Jul 2021", "Jun 2022")                    # overlaps its last 6 months
    assert _years(full_time, overlapping) == 2.5


def test_back_to_back_roles_sharing_a_month_count_it_once():
    # Phase 3 D3: IT Support Apr 2022 - Oct 2022, then Administrator from Oct 2022.
    assert _years(_role("Apr 2022", "Oct 2022"), _role("Oct 2022", "Dec 2023")) == round(21 / 12, 1)


@pytest.mark.parametrize("end", ["Present", "present", "Current", "till date", "", None])
def test_an_ongoing_role_runs_to_the_current_month(end):
    now = datetime.now()
    assert _years(_role(f"{now:%b} {now.year - 2}", end)) == round(25 / 12, 1)   # both end months included


def test_is_current_wins_over_an_end_date():
    now = datetime.now()
    assert _years(_role(f"{now:%b} {now.year - 1}", "Jan 2000", is_current=True)) == round(13 / 12, 1)


@pytest.mark.parametrize("credit, expected", [(1.0, 1.0), (0.5, 0.5), (0.0, 0.0)])
def test_internships_count_as_the_jd_says(credit, expected):
    assert _years(*F1, credit=credit) == expected


def test_an_internship_during_a_job_adds_nothing():
    job = _role("Jan 2025", "Dec 2025")
    assert _years(job, _role("Jun 2025", "Aug 2025", is_internship=True), credit=1.0) == 1.0
    # Only the months outside the job count, at the credit: Jan-Mar 2026 at 50%.
    assert _years(job, _role("Oct 2025", "Mar 2026", is_internship=True), credit=0.5) == round(13.5 / 12, 1)


def test_volunteer_roles_never_count():
    assert _years(_role("Jan 2020", "Dec 2021", is_volunteer=True), credit=1.0) == 0.0
    assert _years(_role("Jan 2020", "Dec 2021", is_internship=True, is_volunteer=True), credit=1.0) == 0.0


def test_unreadable_professional_dates_are_still_data_missing():
    no_dates = _role(None, None)
    assert _years(no_dates) is None
    assert _years(no_dates, *F1, credit=1.0) is None             # credited internships don't hide it
    assert _years(no_dates, _role("Jan 2020", "Dec 2021")) == 2.0  # measured professional time is used


def test_unreadable_professional_dates_fall_back_to_the_stated_total():
    # Seen live: five roles with no dates, the resume states 20 years.
    parsed = {"work_experience": [_role(None, None) for _ in range(5)], "total_experience_years": 20}
    assert experience_years_for_scoring(parsed) == 20.0
    assert experience_years_for_scoring({**parsed, "total_experience_years": None}) is None
    # Measured dates still win over the stated figure.
    measured = {"work_experience": [_role("Jan 2020", "Dec 2021"), _role(None, None)], "total_experience_years": 20}
    assert experience_years_for_scoring(measured) == 2.0


def test_the_resume_display_total_counts_concurrent_roles_once():
    annotated = annotate_work_experience_durations(
        {"work_experience": [_role("Jan 2020", "Dec 2021"), _role("Jun 2020", "Dec 2020"), *F1]})
    assert annotated["total_experience_years"] == 2.0
    assert [e["duration_text"] for e in annotated["work_experience"]] == ["2 yrs", "7 mos", "6 mos", "6 mos"]


# --- the JD's setting ----------------------------------------------------------

def _jd(minimum=1.0, status="CALIBRATED", pct=None, extracted=None):
    return SimpleNamespace(
        calibration_status=SimpleNamespace(value=status), calibrated_min_experience_years=minimum,
        internship_credit_pct=pct, min_experience_years=None,
        extracted_json={"experience": {"min_experience_years": extracted}} if extracted is not None else {},
    )


def test_a_fresher_jd_counts_internships_in_full_by_default():
    assert is_fresher_jd(_jd(1.0)) and is_fresher_jd(_jd(0))
    assert default_internship_credit_pct(_jd(1.0)) == 100 and jd_internship_credit(_jd(1.0)) == 1.0


def test_other_jds_do_not_count_internships_by_default():
    assert not is_fresher_jd(_jd(4.0)) and jd_internship_credit_pct(_jd(4.0)) == 0
    assert not is_fresher_jd(_jd(1.0, status="NOT_CALIBRATED"))      # only a calibrated JD is classed
    assert jd_internship_credit_pct(_jd(1.0, status="NOT_CALIBRATED")) == 0


def test_a_calibrated_jd_without_its_own_minimum_uses_the_extracted_one():
    assert is_fresher_jd(_jd(None, extracted=1)) and not is_fresher_jd(_jd(None, extracted=3))


def test_the_calibrated_setting_wins():
    assert jd_internship_credit_pct(_jd(1.0, pct=50)) == 50
    assert jd_internship_credit_pct(_jd(1.0, pct=0)) == 0
    assert jd_internship_credit_pct(_jd(4.0, pct=100)) == 100


def test_scoring_inputs_count_a_fresher_jds_internships():
    """build_validation_inputs passes the JD's credit: Phase 3 F1 on JD-F (1-year minimum) meets the minimum."""
    from app.services.campaign import validation_inputs as tasks
    config = MagicMock()
    config.get_configs_by_keys.return_value = {}
    jd = SimpleNamespace(min_experience_years=1, extracted_json={}, education_criteria={},
                         experience_tolerance_years=None, calibrated_min_experience_years=1.0,
                         internship_credit_pct=None, calibration_status=SimpleNamespace(value="CALIBRATED"),
                         education_requirement=SimpleNamespace(value="MANDATORY"))
    resume = SimpleNamespace(parsed_json={"work_experience": F1, "education": []})
    experience, *_ = tasks.build_validation_inputs(config, jd, resume)
    assert experience["candidate_years"] == 1.0 and experience["passed"] is True
