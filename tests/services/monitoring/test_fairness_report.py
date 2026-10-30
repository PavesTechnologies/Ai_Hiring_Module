"""The fairness report: regions at state / country level only, gaps, groups, small-group hiding and the ratio."""
import pytest

from app.services.monitoring.fairness_report import (
    NOTES, _groups, career_gap_group, education_group, experience_band, internship_group, longest_gap_months,
)
from app.services.monitoring.regions import region_of


@pytest.mark.parametrize("location, region", [
    ("Chennai", "Tamil Nadu, India"),
    ("Pune, Maharashtra", "Maharashtra, India"),
    ("Imphal", "Manipur, India"),
    ("Colombo, Sri Lanka", "Sri Lanka"),
    ("Dhaka, Bangladesh", "Bangladesh"),
    ("Bengaluru, India", "Karnataka, India"),
    ("Some Village, India", "India"),
    ("Some Village", "Unknown"),
    (None, "Unknown"),
])
def test_the_region_is_a_state_or_a_country_never_the_city(location, region):
    assert region_of(location) == region
    assert "chennai" not in region_of(location).lower() and "pune" not in region.lower()


def _role(start, end):
    return {"title": "Engineer", "start_date": start, "end_date": end}


def test_the_longest_gap_between_roles_ignores_overlaps():
    parsed = {"work_experience": [_role("Jan 2018", "Dec 2019"), _role("Jun 2019", "Jun 2020"),
                                  _role("Sep 2021", "Dec 2022")]}
    assert longest_gap_months(parsed) == 14                         # Jul 2020 - Aug 2021
    assert career_gap_group(parsed) == "Gap over 12 months"
    assert career_gap_group({"work_experience": [_role("Jan 2020", "Jun 2020"), _role("Oct 2020", "Dec 2021")]}) == \
        "No gap over 6 months"
    assert career_gap_group({"work_experience": []}) == "Unknown"


def test_experience_education_and_internship_groups():
    assert [experience_band(y) for y in (0.5, 1, 4.9, 12, None)] == [
        "Under 1 year", "1-3 years", "3-5 years", "10+ years", "Unknown"]
    assert education_group({"education": [{"degree_level": "BACHELOR"}, {"degree_level": "MASTER"}]}) == "Master"
    assert education_group({"education": []}) == "Unknown"
    assert internship_group({"work_experience": [{"is_internship": True}]}) == "Has internships"


def test_small_groups_are_hidden_and_the_ratio_is_to_the_best_shown_group():
    people = ([{"outcome": "PASS", "g": "A"}] * 8 + [{"outcome": "REJECT", "g": "A"}] * 2
              + [{"outcome": "PASS", "g": "B"}] * 5 + [{"outcome": "HOLD", "g": "B"}] * 5
              + [{"outcome": "PASS", "g": "C"}] * 3)
    rows = {r["group"]: r for r in _groups(people, lambda c: c["g"], 10)}
    assert rows["A"]["pass_rate"] == 0.8 and rows["B"]["pass_rate"] == 0.5 and rows["B"]["pass_or_hold_rate"] == 1.0
    assert rows["B"]["ratio_to_best"] == 0.625 and rows["B"]["below_four_fifths"] is True
    assert rows["A"]["ratio_to_best"] == 1.0 and rows["A"]["below_four_fifths"] is False
    assert rows["C"]["suppressed"] and rows["C"]["candidates"] is None and rows["C"]["pass_rate"] is None


def test_the_notes_say_what_it_cannot_show():
    text = " ".join(NOTES).lower()
    for word in ("gender", "age", "caste", "not proof", "minimum experience", "unknown"):
        assert word in text
