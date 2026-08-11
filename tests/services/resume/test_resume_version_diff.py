from app.services.resume.resume_version_diff import (
    diff_education,
    diff_experience,
    diff_experience_years,
    diff_skills,
)

"""S02-T02 - pure diff functions, no DB/service dependencies."""


# ----------------------------------------------------------------------
# diff_skills
# ----------------------------------------------------------------------

def test_diff_skills_added_removed_unchanged():
    result = diff_skills(["Python", "SQL", "AWS"], ["Python", "AWS", "Docker"])

    assert result["added"] == ["Docker"]
    assert result["removed"] == ["SQL"]
    assert result["unchanged"] == ["AWS", "Python"]


def test_diff_skills_is_case_and_whitespace_insensitive():
    result = diff_skills(["python", " AWS "], ["Python", "aws"])

    assert result["added"] == []
    assert result["removed"] == []
    assert result["unchanged"] == ["Python", "aws"]


def test_diff_skills_handles_empty_lists():
    assert diff_skills([], []) == {"added": [], "removed": [], "unchanged": []}
    assert diff_skills([], ["Python"]) == {"added": ["Python"], "removed": [], "unchanged": []}
    assert diff_skills(["Python"], []) == {"added": [], "removed": ["Python"], "unchanged": []}


# ----------------------------------------------------------------------
# diff_experience
# ----------------------------------------------------------------------

def test_diff_experience_matches_by_title_and_company():
    experience_a = [{"title": "Engineer", "company": "Acme"}]
    experience_b = [{"title": "Engineer", "company": "Acme"}, {"title": "Senior Engineer", "company": "Acme"}]

    result = diff_experience(experience_a, experience_b)

    assert result["removed"] == []
    assert result["added"] == [{"title": "Senior Engineer", "company": "Acme"}]


def test_diff_experience_detects_removed_role():
    experience_a = [{"title": "Engineer", "company": "Acme"}]
    experience_b = []

    result = diff_experience(experience_a, experience_b)

    assert result["added"] == []
    assert result["removed"] == [{"title": "Engineer", "company": "Acme"}]


def test_diff_experience_same_title_different_company_is_not_a_match():
    experience_a = [{"title": "Engineer", "company": "Acme"}]
    experience_b = [{"title": "Engineer", "company": "Globex"}]

    result = diff_experience(experience_a, experience_b)

    assert result["added"] == [{"title": "Engineer", "company": "Globex"}]
    assert result["removed"] == [{"title": "Engineer", "company": "Acme"}]


def test_diff_experience_is_case_insensitive_on_match_key():
    experience_a = [{"title": "engineer", "company": "acme"}]
    experience_b = [{"title": "Engineer", "company": "Acme"}]

    result = diff_experience(experience_a, experience_b)

    assert result["added"] == []
    assert result["removed"] == []


# ----------------------------------------------------------------------
# diff_education
# ----------------------------------------------------------------------

def test_diff_education_added_and_removed():
    education_a = [{"degree": "BSc", "institution": "MIT", "field": "CS", "graduation_year": 2018}]
    education_b = [
        {"degree": "BSc", "institution": "MIT", "field": "CS", "graduation_year": 2018},
        {"degree": "MSc", "institution": "Stanford", "field": "AI", "graduation_year": 2020},
    ]

    result = diff_education(education_a, education_b)

    assert result["removed"] == []
    assert result["added"] == [{"degree": "MSc", "institution": "Stanford", "field": "AI", "graduation_year": 2020}]


def test_diff_education_no_changes_when_identical():
    education = [{"degree": "BSc", "institution": "MIT", "field": "CS", "graduation_year": 2018}]

    result = diff_education(education, education)

    assert result == {"added": [], "removed": []}


# ----------------------------------------------------------------------
# diff_experience_years
# ----------------------------------------------------------------------

def test_diff_experience_years_computes_positive_difference():
    result = diff_experience_years(3.0, 5.5)

    assert result == {"version_1": 3.0, "version_2": 5.5, "difference": 2.5}


def test_diff_experience_years_computes_negative_difference():
    result = diff_experience_years(5.0, 2.0)

    assert result["difference"] == -3.0


def test_diff_experience_years_none_when_either_side_missing():
    assert diff_experience_years(None, 5.0)["difference"] is None
    assert diff_experience_years(5.0, None)["difference"] is None
    assert diff_experience_years(None, None)["difference"] is None
