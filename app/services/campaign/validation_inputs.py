"""
The deterministic layer's non-skill inputs for a resume against a JD: experience, education (with the
JD calibration applied) and the functional (domain capability) check. Shared by the scoring tasks, the
golden runner and the comparison scripts (moved out of deterministic_scoring_tasks in retire v1, phase E).
"""
from app.services.campaign.domain_capability_matching_service import DomainCapabilityMatchingService
from app.services.campaign.experience_education_validation_service import ExperienceEducationValidationService
from app.services.education.education_field_family_service import field_families_for_jd
from app.services.jd.jd_calibration_rules import (
    apply_education_requirement, apply_experience_tolerance, experience_minimum_inputs, jd_internship_credit,
)
from app.services.resume.work_experience_duration import experience_years_for_scoring

_EXPERIENCE_TOLERANCE_YEARS_KEY = "EXPERIENCE_TOLERANCE_YEARS"
_EQUIVALENT_EXPERIENCE_YEARS_KEY = "EQUIVALENT_EXPERIENCE_YEARS"
_DETERMINISTIC_WEIGHT_SKILLS_KEY = "DETERMINISTIC_WEIGHT_SKILLS"
_DETERMINISTIC_WEIGHT_EXPERIENCE_KEY = "DETERMINISTIC_WEIGHT_EXPERIENCE"
_DETERMINISTIC_WEIGHT_EDUCATION_KEY = "DETERMINISTIC_WEIGHT_EDUCATION"
_DETERMINISTIC_WEIGHT_FUNCTIONAL_KEY = "DETERMINISTIC_WEIGHT_FUNCTIONAL"


def build_validation_inputs(config_repo, job_description, resume):
    """
    (experience_result, education_result, score_weights, domain_result) for a
    resume against a JD - the deterministic layer's non-skill inputs, shared
    by the scoring tasks, the golden runner and the comparison scripts.
    """
    parsed_json = resume.parsed_json or {}
    # Same "JSON is the single source of truth" preference as
    # education/JD-experience above: prefer total_experience_years
    # computed from work_experience's own start_date/end_date (the same
    # computation the resume-parsed-json display endpoint already
    # applies, per annotate_work_experience_durations' own docstring -
    # it doesn't trust the AI-extracted figure verbatim since that can
    # drift from what the listed dates actually add up to) over the raw
    # extracted field, which is null whenever the resume never states an
    # explicit "X years" figure even though its dates make one computable.
    # Nothing counted (no roles, or only uncredited internships) is 0 years,
    # not missing data. Internships count as the JD's calibration says.
    candidate_total_years = experience_years_for_scoring(parsed_json, jd_internship_credit(job_description))
    candidate_education_entries = parsed_json.get("education")
    required_degree_text = (job_description.education_criteria or {}).get("degree")
    jd_extracted_education = (job_description.extracted_json or {}).get("education")

    weight_configs = config_repo.get_configs_by_keys([
        _EXPERIENCE_TOLERANCE_YEARS_KEY,
        _EQUIVALENT_EXPERIENCE_YEARS_KEY,
        _DETERMINISTIC_WEIGHT_SKILLS_KEY,
        _DETERMINISTIC_WEIGHT_EXPERIENCE_KEY,
        _DETERMINISTIC_WEIGHT_EDUCATION_KEY,
        _DETERMINISTIC_WEIGHT_FUNCTIONAL_KEY,
    ])
    validation_service = ExperienceEducationValidationService(
        experience_tolerance_years=float(weight_configs.get(_EXPERIENCE_TOLERANCE_YEARS_KEY, 0.0)),
        equivalent_experience_years=float(weight_configs[_EQUIVALENT_EXPERIENCE_YEARS_KEY])
        if weight_configs.get(_EQUIVALENT_EXPERIENCE_YEARS_KEY) is not None else None,
    )
 
    min_experience_years = (
        float(job_description.min_experience_years)
        if job_description.min_experience_years is not None else None
    )
    # JD calibration: the calibrated minimum first, else the extraction / column.
    min_experience_years, jd_extracted_experience = experience_minimum_inputs(job_description, min_experience_years)
    experience_result = validation_service.validate_experience(
        min_experience_years, candidate_total_years,
        jd_extracted_experience=jd_extracted_experience,
    )
    education_result = validation_service.validate_education(
        required_degree_text, candidate_education_entries, candidate_total_years,
        jd_extracted_education=jd_extracted_education,
        required_field_text=(job_description.education_criteria or {}).get("field"),
        # Related fields for the JD's role domain, when its education is mandatory.
        field_families=field_families_for_jd(config_repo.db, job_description),
    )
    # JD calibration: the near-miss tolerance and education mandatory/preferred.
    experience_result = apply_experience_tolerance(experience_result, job_description)
    education_result = apply_education_requirement(education_result, job_description)
    score_weights = {
        "skills": float(weight_configs.get(_DETERMINISTIC_WEIGHT_SKILLS_KEY, 0.70)),
        "experience": float(weight_configs.get(_DETERMINISTIC_WEIGHT_EXPERIENCE_KEY, 0.15)),
        "education": float(weight_configs.get(_DETERMINISTIC_WEIGHT_EDUCATION_KEY, 0.15)),
        "functional": float(weight_configs.get(_DETERMINISTIC_WEIGHT_FUNCTIONAL_KEY, 0.15)),
    }
    domain_result = DomainCapabilityMatchingService().evaluate(job_description.extracted_json, parsed_json)
    return experience_result, education_result, score_weights, domain_result
