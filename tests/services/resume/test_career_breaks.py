"""
A stated career break - career break, family care, sabbatical, parental leave, caregiving - is not
experience. A resume may list one like a role, and a parse may keep it as a work_experience entry:
it is never counted towards the minimum (v1 and v2 both read experience_years_for_scoring), never
in the displayed total, and in the fairness report its months are a gap.
"""
import pytest

from app.services.monitoring.fairness_report import career_gap_group, longest_gap_months
from app.services.resume.work_experience_duration import (
    annotate_work_experience_durations, experience_years_for_scoring, is_career_break,
)


def _role(start, end, title="Data Engineer", company="Acme Analytics", **extra):
    return {"title": title, "company": company, "start_date": start, "end_date": end, **extra}


@pytest.mark.parametrize("title, company", [
    ("Career Break", None),
    ("Career break - relocation", ""),
    ("Family care", None),
    ("Sabbatical", None),
    ("Parental leave", None),
    ("Maternity leave", None),
    ("Paternity Leave", None),
    ("Caregiving", None),
    ("Full-time caregiver", None),
    (None, "Career-break"),
    ("Planned break", "Sabbatical"),
])
def test_a_stated_break_is_recognised_from_its_title_or_company(title, company):
    assert is_career_break({"title": title, "company": company})


def test_the_description_counts_only_when_the_entry_names_nothing():
    assert is_career_break({"title": None, "company": None, "description": "Took a career break to care for family."})
    # A real job that mentions caregivers or sabbaticals is still a job.
    assert not is_career_break(_role("Jan 2020", "Dec 2021", title="Product Manager", company="CareConnect",
                                     description="Built a scheduling app for caregivers; ran the sabbatical policy."))


@pytest.mark.parametrize("title", [
    "Data Engineer", "Caretaker of build pipelines", "Family Office Analyst", "Leave management lead",
    "Software Engineer - Careers site", "Breakfast cook",
])
def test_ordinary_titles_are_not_breaks(title):
    assert not is_career_break(_role("Jan 2020", "Dec 2021", title=title))


# The S2 counterfactual: a senior data engineer near the 12-year minimum, with a year away.
BEFORE = _role("Oct 2010", "Sep 2015", title="Data Engineer", company="Northwind")
AFTER = _role("Oct 2021", "Sep 2024", title="Lead Data Engineer", company="Contoso")


def test_a_stated_parental_leave_counts_the_same_as_a_silent_gap():
    silent = {"work_experience": [BEFORE, _role("Oct 2015", "Sep 2020", title="Senior Data Engineer"), AFTER]}
    stated = {"work_experience": [BEFORE, _role("Oct 2015", "Sep 2020", title="Senior Data Engineer"),
                                  _role("Oct 2020", "Sep 2021", title="Parental leave", company=None), AFTER]}

    assert experience_years_for_scoring(silent) == 13.0
    assert experience_years_for_scoring(stated) == experience_years_for_scoring(silent)
    assert annotate_work_experience_durations(stated)["total_experience_years"] == 13.0


def test_a_break_is_not_internship_credit_either():
    entries = [_role("Jan 2024", "Jun 2024", title="Data Intern", is_internship=True),
               _role("Jul 2024", "Dec 2024", title="Sabbatical", company=None, is_internship=True)]
    assert experience_years_for_scoring({"work_experience": entries}, internship_credit=1.0) == 0.5


def test_only_a_break_is_zero_years_not_missing():
    assert experience_years_for_scoring({"work_experience": [_role("Jan 2023", "Dec 2023", title="Career break")],
                                         "total_experience_years": 1}) == 0.0


def test_the_annotated_entry_keeps_its_duration_for_display():
    [entry] = annotate_work_experience_durations(
        {"work_experience": [_role("Jan 2023", "Dec 2023", title="Career break")]})["work_experience"]
    assert entry["duration_text"] == "1 yr"


def test_a_breaks_months_are_a_gap_in_the_fairness_report():
    stated = {"work_experience": [_role("Jan 2018", "Dec 2019"),
                                  _role("Jan 2020", "Jun 2021", title="Family care", company=None),
                                  _role("Jul 2021", "Dec 2022")]}
    assert longest_gap_months(stated) == 18
    assert career_gap_group(stated) == "Gap over 12 months"
