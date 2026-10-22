"""
How a JD's calibration settings shape the experience / education results that
scoring (v1 and v2) consumes. Pure functions over the validation results, so
every place that validates experience/education applies them the same way.

- experience: the JD's calibrated near-miss tolerance travels with the result
  (near_miss.evaluate_near_miss reads it); unset = the default
  max(1 year, 10% of the minimum).
- education: PREFERRED is never a gate - the result is marked passed, with the
  real outcome kept in requirement_met for display. Scoring v2 counts it as a
  small score component; MANDATORY stays a gate only.
- internships: how much internship time counts as experience (100 / 50 / 0%),
  as calibrated; unset = 100% on a fresher JD (calibrated minimum <= 1 year),
  else 0%.
- role domain: set in calibration; scopes the related education fields that
  count when education is mandatory (education_field_family_service).
- profile template (Fresher / Experienced / Domain, jd_profile_templates):
  chosen in calibration; a setting the JD leaves unset (internship credit,
  tolerance) takes the template's default before the rules above.
  Education is always set on the JD - the template only prefills the form.
"""
from decimal import Decimal

EDUCATION_MANDATORY = "MANDATORY"
EDUCATION_PREFERRED = "PREFERRED"


INTERNSHIP_CREDIT_OPTIONS = (100, 50, 0)
FRESHER_MAX_MIN_YEARS = 1.0
FRESHER_INTERNSHIP_CREDIT_PCT = 100
DEFAULT_INTERNSHIP_CREDIT_PCT = 0


def is_fresher_jd(job_description) -> bool:
    """A calibrated JD whose minimum (calibrated, else extracted) is at most 1 year."""
    status = getattr(getattr(job_description, "calibration_status", None), "value",
                     getattr(job_description, "calibration_status", None))
    if status != "CALIBRATED":
        return False
    minimum = calibrated_min_experience(job_description)
    if minimum is None:
        extracted = ((getattr(job_description, "extracted_json", None) or {}).get("experience") or {}).get(
            "min_experience_years")
        column = getattr(job_description, "min_experience_years", None)
        value = extracted if extracted is not None else column
        minimum = float(value) if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool) else None
    return minimum is not None and minimum <= FRESHER_MAX_MIN_YEARS


def jd_profile(job_description):
    """The JD's profile template row (jd_profile_templates), else None."""
    key = getattr(job_description, "profile_template", None)
    if not isinstance(key, str):
        return None
    profile = getattr(job_description, "profile", None)
    return profile if getattr(profile, "key", None) == key else None


def default_internship_credit_pct(job_description) -> int:
    """The profile template's default, else 100 on a fresher JD, else 0."""
    value = getattr(jd_profile(job_description), "internship_credit_pct", None)
    if isinstance(value, int) and not isinstance(value, bool) and value in INTERNSHIP_CREDIT_OPTIONS:
        return value
    return FRESHER_INTERNSHIP_CREDIT_PCT if is_fresher_jd(job_description) else DEFAULT_INTERNSHIP_CREDIT_PCT


def jd_internship_credit_pct(job_description) -> int:
    """The calibrated setting, else the default."""
    value = getattr(job_description, "internship_credit_pct", None)
    if isinstance(value, int) and not isinstance(value, bool) and value in INTERNSHIP_CREDIT_OPTIONS:
        return value
    return default_internship_credit_pct(job_description)


def jd_internship_credit(job_description) -> float:
    """The share of internship time counted as experience (1.0 / 0.5 / 0.0)."""
    return jd_internship_credit_pct(job_description) / 100


ROLE_DOMAINS = ("SOFTWARE", "IT_OPERATIONS", "DATA", "BUSINESS", "OTHER")


def jd_role_domain(job_description) -> str | None:
    """The calibrated role domain, else None."""
    value = getattr(job_description, "role_domain", None)
    return value if isinstance(value, str) and value in ROLE_DOMAINS else None


def _years(value) -> float | None:
    return float(value) if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool) else None


def jd_tolerance_years(job_description) -> float | None:
    """The calibrated tolerance, else the profile template's, else None (the default)."""
    value = _years(getattr(job_description, "experience_tolerance_years", None))
    if value is not None:
        return value
    return _years(getattr(jd_profile(job_description), "experience_tolerance_years", None))


def jd_education_requirement(job_description) -> str:
    value = getattr(job_description, "education_requirement", None)
    value = getattr(value, "value", value)
    return EDUCATION_PREFERRED if value == EDUCATION_PREFERRED else EDUCATION_MANDATORY


def apply_experience_tolerance(experience_result: dict | None, job_description) -> dict | None:
    if experience_result is None:
        return None
    return {**experience_result, "near_miss_tolerance_years": jd_tolerance_years(job_description)}


def apply_education_requirement(education_result: dict | None, job_description) -> dict | None:
    if education_result is None:
        return None
    requirement = jd_education_requirement(job_description)
    if requirement == EDUCATION_PREFERRED:
        return {**education_result, "requirement": EDUCATION_PREFERRED,
                "requirement_met": education_result.get("passed"), "passed": True}
    return {**education_result, "requirement": EDUCATION_MANDATORY}


def jd_calibration_response_fields(jd) -> dict:
    """Calibration fields for JD API responses (the main readiness badge)."""
    status = getattr(jd, "calibration_status", None)
    status = getattr(status, "value", status)
    version = getattr(jd, "calibration_version", 0)
    return {
        "calibration_status": status if status in ("CALIBRATED", "NOT_CALIBRATED") else "NOT_CALIBRATED",
        "calibration_version": version if isinstance(version, int) and not isinstance(version, bool) else 0,
        "calibrated_at": getattr(jd, "calibrated_at", None) if status == "CALIBRATED" else None,
    }


def calibrated_min_experience(job_description) -> float | None:
    """The calibrated minimum on a calibrated JD, else None."""
    status = getattr(getattr(job_description, "calibration_status", None), "value",
                     getattr(job_description, "calibration_status", None))
    value = getattr(job_description, "calibrated_min_experience_years", None)
    if status != "CALIBRATED" or not isinstance(value, (int, float, Decimal)) or isinstance(value, bool):
        return None
    return float(value)


def experience_minimum_inputs(job_description, column_min: float | None = None) -> tuple[float | None, dict | None]:
    """
    (min_experience_years, jd_extracted_experience) for validate_experience.
    The calibrated minimum wins - passed with no extraction so it isn't
    overridden; otherwise the extraction (then the JD column) as before. The
    extraction itself is never changed.
    """
    calibrated = calibrated_min_experience(job_description)
    if calibrated is not None:
        return calibrated, None
    return column_min, (getattr(job_description, "extracted_json", None) or {}).get("experience")
