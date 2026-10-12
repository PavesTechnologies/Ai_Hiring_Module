"""
Fills gaps in a JD's AI-extracted education from the education_criteria the
recruiter entered at upload (the JD form's {"degree", "field"}).

The AI extraction stays the primary source - a value it returned is never
overwritten. Only what it left empty is filled: a null degree/field from
the form text, an UNKNOWN degree_level/field_normalized by deterministic
classification (same classifiers EducationMatchingService uses), and
related_field_allowed from the form's field text. The result is always an
education object, never null, so scoring and the UI always have the same
shape to read.

Which keys were filled is recorded in `gap_filled_fields`, so a later
metadata-only update (recruiter edits education_criteria, JD text
unchanged) can re-fill them from the new form values instead of treating
the old fills as AI output.
"""
from app.enums.education import DegreeLevel, EducationField
from app.services.education.education_matching_service import (
    classify_degree_level,
    classify_field,
    detect_related_field_allowed,
)

GAP_FILLED_KEY = "gap_filled_fields"

_DEFAULTS = {
    "degree": None,
    "field": None,
    "degree_level": DegreeLevel.UNKNOWN.value,
    "field_normalized": EducationField.UNKNOWN.value,
    "related_field_allowed": False,
}


def _clean(text) -> str | None:
    if not isinstance(text, str):
        return None
    return text.strip() or None


def _valid(value, enum_cls) -> str:
    try:
        return enum_cls(value).value
    except ValueError:
        return enum_cls.UNKNOWN.value


def merge_education_with_criteria(extracted_education: dict | None, education_criteria: dict | None) -> dict:
    education = dict(extracted_education or {})

    # Undo previous gap-fills first - they came from the form, not the AI,
    # so they must follow the current form values, not stick.
    for key in education.pop(GAP_FILLED_KEY, None) or []:
        if key in _DEFAULTS:
            education[key] = _DEFAULTS[key]

    for key, default in _DEFAULTS.items():
        education.setdefault(key, default)
    education["degree"] = _clean(education["degree"])
    education["field"] = _clean(education["field"])
    education["degree_level"] = _valid(education["degree_level"], DegreeLevel)
    education["field_normalized"] = _valid(education["field_normalized"], EducationField)
    education["related_field_allowed"] = bool(education["related_field_allowed"])

    criteria = education_criteria or {}
    form_degree = _clean(criteria.get("degree"))
    form_field = _clean(criteria.get("field"))
    filled: list[str] = []

    if education["degree"] is None and form_degree:
        education["degree"] = form_degree
        filled.append("degree")
    if education["field"] is None and form_field:
        education["field"] = form_field
        filled.append("field")

    if education["degree_level"] == DegreeLevel.UNKNOWN.value:
        for text in (form_degree, education["degree"]):
            level = classify_degree_level(text)
            if level != DegreeLevel.UNKNOWN:
                education["degree_level"] = level.value
                filled.append("degree_level")
                break

    if education["field_normalized"] == EducationField.UNKNOWN.value:
        for text in (form_field, education["field"]):
            field = classify_field(text)
            if field != EducationField.UNKNOWN:
                education["field_normalized"] = field.value
                filled.append("field_normalized")
                break

    # Read from the field text actually in use - the form's "or related"
    # must not attach to a different field the AI extracted.
    if not education["related_field_allowed"] and detect_related_field_allowed(education["field"]):
        education["related_field_allowed"] = True
        filled.append("related_field_allowed")

    if filled:
        education[GAP_FILLED_KEY] = filled
    return education


def merge_extracted_json_education(extracted_json: dict | None, education_criteria: dict | None) -> dict | None:
    """extracted_json with its `education` gap-filled - None stays None (no AI extraction to fill)."""
    if extracted_json is None:
        return None
    return {**extracted_json, "education": merge_education_with_criteria(extracted_json.get("education"), education_criteria)}
