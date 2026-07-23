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
    assert result["score"] == round((3 / 4) * 100, 2)  # BACHELOR(3) / MASTER(4)


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
