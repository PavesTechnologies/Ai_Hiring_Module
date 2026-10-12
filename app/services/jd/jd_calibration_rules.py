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
"""
from decimal import Decimal

EDUCATION_MANDATORY = "MANDATORY"
EDUCATION_PREFERRED = "PREFERRED"


def jd_tolerance_years(job_description) -> float | None:
    value = getattr(job_description, "experience_tolerance_years", None)
    return float(value) if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool) else None


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
