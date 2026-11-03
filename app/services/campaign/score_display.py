"""
Display helpers for scores and their checks - degree levels, fields of study, education gaps, years,
optional numbers. Shared by the scorecard, the campaign views and rejection texts (moved out of the v1
scoring service in retire v1, phase E).
"""


def _optional_float(value) -> float | None:
    """float(value), or None when unset - campaign rows always have it; older callers/fixtures may not."""
    return None if value is None else float(value)


# M07-E03 S01 T02: human-readable display names for
# ExperienceEducationValidationService's internal degree-level names -
# rejection_reason must never surface an internal level code verbatim.
# Matches app.enums.education.DEGREE_LEVEL_RANK's keys (the only levels
# ExperienceEducationValidationService can ever produce, now that it
# sources degree_level from DegreeLevel/DEGREE_LEVEL_RANK - PROFESSIONAL/
# OTHER/UNKNOWN are deliberately unranked there and never reach here).
_DEGREE_LEVEL_DISPLAY_NAMES = {
    "CERTIFICATE": "Certificate",
    "DIPLOMA": "Diploma",
    "ASSOCIATE": "Associate's",
    "BACHELOR": "Bachelor's",
    "POSTGRADUATE_DIPLOMA": "Postgraduate Diploma",
    "MASTER": "Master's",
    "DOCTORATE": "Doctorate",
}


def _degree_level_display(level_name: str | None) -> str:
    if level_name is None:
        return "no qualifying degree"
    return _DEGREE_LEVEL_DISPLAY_NAMES.get(level_name, level_name.replace("_", " ").title())


def _field_display(code: str | None, text: str | None) -> str:
    """The raw field text when there is one, else the readable enum name ('COMPUTER_SCIENCE' -> 'Computer Science')."""
    if text:
        return text
    if code:
        return code.replace("_", " ").title()
    return "no matching field"


def describe_education_gap(education_result: dict | None) -> tuple[str | None, str | None]:
    """
    (degree-level gap, field-of-study gap) for a failed education result,
    each "X required, Y found" or None when that half passed. A breakdown
    stored before field-of-study matching has no level_passed/field_passed -
    its failure was always the degree level.
    """
    if not education_result or education_result.get("passed"):
        return None, None
    level_gap = None
    if education_result.get("level_passed") is not True:
        level_gap = (
            f"{_degree_level_display(education_result.get('required_level'))} required, "
            f"{_degree_level_display(education_result.get('candidate_level'))} found"
        )
    field_gap = None
    if education_result.get("field_passed") is False:
        field_gap = (
            f"{_field_display(education_result.get('required_field'), education_result.get('required_field_text'))}"
            f" required, "
            f"{_field_display(education_result.get('candidate_field'), education_result.get('candidate_field_text'))}"
            f" found"
        )
    return level_gap, field_gap


def _format_years(value: float) -> str:
    """4.0 -> '4', 2.5 -> '2.5' - matches the ticket's example wording."""
    rounded = round(value, 1)
    return str(int(rounded)) if rounded == int(rounded) else str(rounded)
