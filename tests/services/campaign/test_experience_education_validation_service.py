from app.services.campaign.experience_education_validation_service import (
    ExperienceEducationValidationService,
)

# ---------------------------------------------------------------- S01: Experience


def test_experience_skipped_when_jd_has_no_minimum():
    service = ExperienceEducationValidationService()
    result = service.validate_experience(min_experience_years=None, candidate_total_years=3.0)

    assert result["applicable"] is False
    assert result["skipped"] is True
    assert result["data_missing"] is False
    assert result["passed"] is True
    assert result["score"] == 100.0


def test_experience_data_missing_when_resume_has_no_years():
    service = ExperienceEducationValidationService()
    result = service.validate_experience(min_experience_years=5.0, candidate_total_years=None)

    assert result["applicable"] is False
    assert result["skipped"] is False
    assert result["data_missing"] is True
    assert result["passed"] is True
    assert result["score"] is None


def test_experience_passes_when_candidate_meets_minimum():
    service = ExperienceEducationValidationService()
    result = service.validate_experience(min_experience_years=5.0, candidate_total_years=6.0)

    assert result["applicable"] is True
    assert result["passed"] is True
    assert result["score"] == 100.0


def test_experience_fails_when_below_minimum_with_zero_tolerance():
    service = ExperienceEducationValidationService(experience_tolerance_years=0.0)
    result = service.validate_experience(min_experience_years=5.0, candidate_total_years=3.0)

    assert result["passed"] is False
    assert result["effective_min_years"] == 5.0
    assert result["score"] == round((3.0 / 5.0) * 100, 2)


def test_experience_tolerance_allows_a_configurable_shortfall():
    service = ExperienceEducationValidationService(experience_tolerance_years=1.0)
    result = service.validate_experience(min_experience_years=5.0, candidate_total_years=4.2)

    assert result["effective_min_years"] == 4.0
    assert result["passed"] is True
    assert result["score"] == 100.0


# ---------------------------------------------------------------- S02: Education


def test_education_skipped_when_jd_has_no_degree_requirement():
    service = ExperienceEducationValidationService()
    result = service.validate_education(None, [{"degree": "Bachelor's"}], candidate_total_years=2.0)

    assert result["applicable"] is False
    assert result["skipped"] is True
    assert result["passed"] is True
    assert result["score"] == 100.0


def test_education_passes_for_exact_required_level():
    service = ExperienceEducationValidationService()
    result = service.validate_education(
        "Bachelor's Degree", [{"degree": "Bachelor of Science"}], candidate_total_years=2.0,
    )

    assert result["passed"] is True
    assert result["required_level"] == "BACHELOR"
    assert result["candidate_level"] == "BACHELOR"
    assert result["score"] == 100.0


def test_education_higher_degree_satisfies_lower_requirement():
    """
    'Multiple acceptable degrees' falls out of the hierarchy: a JD requiring
    Bachelor's must also accept Master's/PhD, without the JD schema needing
    an explicit list of acceptable degrees.
    """
    service = ExperienceEducationValidationService()
    result = service.validate_education(
        "Bachelor's", [{"degree": "Master of Science in Computer Science"}], candidate_total_years=2.0,
    )

    assert result["passed"] is True
    assert result["candidate_level"] == "MASTER"


def test_education_fails_for_lower_degree_without_equivalent_experience():
    service = ExperienceEducationValidationService(equivalent_experience_years=8.0)
    result = service.validate_education(
        "Master's", [{"degree": "Bachelor's"}], candidate_total_years=2.0,
    )

    assert result["passed"] is False
    assert result["equivalent_experience_applied"] is False
    assert result["score"] == round((4 / 6) * 100, 2)  # BACHELOR(4) / MASTER(6) - app.enums.education.DEGREE_LEVEL_RANK


def test_equivalent_experience_substitutes_for_insufficient_degree():
    service = ExperienceEducationValidationService(equivalent_experience_years=8.0)
    result = service.validate_education(
        "Master's", [{"degree": "Bachelor's"}], candidate_total_years=9.0,
    )

    assert result["passed"] is True
    assert result["equivalent_experience_applied"] is True
    assert result["score"] == 100.0


def test_equivalent_experience_substitutes_when_no_education_parsed_at_all():
    service = ExperienceEducationValidationService(equivalent_experience_years=8.0)
    result = service.validate_education("Bachelor's", [], candidate_total_years=10.0)

    assert result["applicable"] is True
    assert result["data_missing"] is True
    assert result["passed"] is True
    assert result["equivalent_experience_applied"] is True


def test_education_data_missing_when_no_entries_and_insufficient_experience():
    service = ExperienceEducationValidationService(equivalent_experience_years=8.0)
    result = service.validate_education("Bachelor's", None, candidate_total_years=1.0)

    assert result["applicable"] is False
    assert result["data_missing"] is True
    assert result["passed"] is True
    assert result["score"] is None


# ---------------------------------------------------------------- Field of study

_JD_CS = {"degree": "B.Tech", "field": "Computer Science", "degree_level": "BACHELOR",
          "field_normalized": "COMPUTER_SCIENCE", "related_field_allowed": False}


def _entry(degree="B.Tech", field=None, degree_level="BACHELOR", field_normalized="UNKNOWN"):
    return {"degree": degree, "field": field, "degree_level": degree_level, "field_normalized": field_normalized}


def test_field_exact_match_passes_full_score():
    service = ExperienceEducationValidationService()
    result = service.validate_education(
        None, [_entry(field="Computer Science", field_normalized="COMPUTER_SCIENCE")], 2.0,
        jd_extracted_education=_JD_CS,
    )

    assert result["passed"] is True
    assert result["field_match"] == "FULL_MATCH"
    assert result["level_passed"] is True and result["field_passed"] is True
    assert result["score"] == 100.0


def test_field_mismatch_fails_even_when_degree_level_meets_requirement():
    service = ExperienceEducationValidationService(equivalent_experience_years=8.0)
    result = service.validate_education(
        None, [_entry(field="Mechanical Engineering", field_normalized="MECHANICAL_ENGINEERING")], 2.0,
        jd_extracted_education=_JD_CS,
    )

    assert result["passed"] is False
    assert result["field_match"] == "MISMATCH"
    assert result["level_passed"] is True
    assert result["field_passed"] is False
    assert result["score"] == 50.0


def test_related_field_passes_only_when_jd_allows_it():
    service = ExperienceEducationValidationService()
    it_entry = [_entry(field="Information Technology", field_normalized="INFORMATION_TECHNOLOGY")]

    strict = service.validate_education(None, it_entry, 2.0, jd_extracted_education=_JD_CS)
    relaxed = service.validate_education(
        None, it_entry, 2.0, jd_extracted_education={**_JD_CS, "related_field_allowed": True},
    )

    assert strict["passed"] is False and strict["field_match"] == "MISMATCH"
    assert relaxed["passed"] is True and relaxed["field_match"] == "RELATED_FIELD_MATCH"
    assert relaxed["score"] == 90.0


def test_best_entry_chosen_across_degree_and_field():
    service = ExperienceEducationValidationService()
    entries = [
        _entry(degree="B.Tech", field="Mechanical", field_normalized="MECHANICAL_ENGINEERING"),
        _entry(degree="M.Tech", degree_level="MASTER", field="Computer Science", field_normalized="COMPUTER_SCIENCE"),
    ]

    result = service.validate_education(None, entries, 2.0, jd_extracted_education=_JD_CS)

    assert result["passed"] is True
    assert result["candidate_level"] == "MASTER"
    assert result["field_match"] == "FULL_MATCH"
    assert result["candidate_degree_text"] == "M.Tech"


def test_unclassifiable_candidate_field_is_not_held_against_them():
    service = ExperienceEducationValidationService()
    result = service.validate_education(None, [_entry(field="Liberal studies")], 2.0, jd_extracted_education=_JD_CS)

    assert result["passed"] is True
    assert result["field_match"] == "UNKNOWN"


def test_field_only_requirement_is_checked_from_form_text():
    service = ExperienceEducationValidationService()
    result = service.validate_education(
        None, [_entry(degree="BCA", field="Information Technology")], 2.0,
        required_field_text="Computer Science or related field",
    )

    assert result["skipped"] is False
    assert result["required_field"] == "COMPUTER_SCIENCE"
    assert result["related_field_allowed"] is True
    assert result["field_match"] == "RELATED_FIELD_MATCH"
    assert result["passed"] is True


def test_equivalent_experience_substitutes_for_field_mismatch():
    service = ExperienceEducationValidationService(equivalent_experience_years=8.0)
    result = service.validate_education(
        None, [_entry(field="Civil Engineering", field_normalized="CIVIL_ENGINEERING")], 10.0,
        jd_extracted_education=_JD_CS,
    )

    assert result["passed"] is True
    assert result["equivalent_experience_applied"] is True
    assert result["score"] == 100.0


def test_no_field_requirement_keeps_degree_only_behavior():
    service = ExperienceEducationValidationService()
    result = service.validate_education(
        None, [_entry(field="History", field_normalized="OTHER")], 2.0,
        jd_extracted_education={**_JD_CS, "field": None, "field_normalized": "UNKNOWN"},
    )

    assert result["passed"] is True
    assert result["field_match"] == "NOT_REQUIRED"
    assert result["score"] == 100.0
