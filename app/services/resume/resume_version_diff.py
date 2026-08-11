"""
S02-T02 - pure, framework-agnostic diff functions comparing two resume
versions' parsed_json. Mirrors work_experience_duration.py's convention of
plain dict in / plain dict out, computed fresh on every call - nothing here
is persisted, and this module never touches the database.
"""


def _normalize(value) -> str:
    return (value or "").strip().casefold()


def diff_skills(skills_a: list[str], skills_b: list[str]) -> dict:
    """
    Case/whitespace-insensitive set diff (parsed_json skills are raw
    AI-extracted strings, not canonical skill_ontology ids, so "Python" and
    "python" across two parse runs must not show up as a spurious
    added+removed pair). The surviving display casing always comes from
    whichever side has it; unchanged skills show version_b's casing.
    """
    by_key_a = {_normalize(s): s for s in skills_a if s}
    by_key_b = {_normalize(s): s for s in skills_b if s}
    return {
        "added": sorted(by_key_b[key] for key in by_key_b if key not in by_key_a),
        "removed": sorted(by_key_a[key] for key in by_key_a if key not in by_key_b),
        "unchanged": sorted(by_key_b[key] for key in by_key_b if key in by_key_a),
    }


def _experience_key(entry: dict) -> tuple:
    return (_normalize(entry.get("title")), _normalize(entry.get("company")))


def diff_experience(experience_a: list[dict], experience_b: list[dict]) -> dict:
    """Matched by (title, company) per spec - no "unchanged" bucket requested for experience."""
    keys_a = {_experience_key(entry) for entry in experience_a}
    keys_b = {_experience_key(entry) for entry in experience_b}
    return {
        "added": [entry for entry in experience_b if _experience_key(entry) not in keys_a],
        "removed": [entry for entry in experience_a if _experience_key(entry) not in keys_b],
    }


def _education_key(entry: dict) -> tuple:
    return (
        _normalize(entry.get("degree")),
        _normalize(entry.get("institution")),
        _normalize(entry.get("field")),
        entry.get("graduation_year"),
    )


def diff_education(education_a: list[dict], education_b: list[dict]) -> dict:
    """No match key specified by spec - a full-entry equality diff (degree+institution+field+year)."""
    keys_a = {_education_key(entry) for entry in education_a}
    keys_b = {_education_key(entry) for entry in education_b}
    return {
        "added": [entry for entry in education_b if _education_key(entry) not in keys_a],
        "removed": [entry for entry in education_a if _education_key(entry) not in keys_b],
    }


def diff_experience_years(years_a: float | None, years_b: float | None) -> dict:
    """version_b - version_a; None when either side has no extracted value rather than treating missing as zero."""
    difference = round(years_b - years_a, 2) if years_a is not None and years_b is not None else None
    return {"version_1": years_a, "version_2": years_b, "difference": difference}
