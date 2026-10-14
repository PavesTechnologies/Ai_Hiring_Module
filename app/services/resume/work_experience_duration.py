from datetime import datetime

from dateutil import parser as date_parser

_CURRENT_MARKERS = {"present", "current", "till date", "ongoing", "now", "till now"}
_DEFAULT_ANCHOR = datetime(2000, 1, 1)


def _parse_month(date_str: str | None) -> datetime | None:
    if not date_str or not date_str.strip():
        return None
    try:
        return date_parser.parse(date_str.strip(), default=_DEFAULT_ANCHOR, fuzzy=True)
    except (ValueError, OverflowError):
        return None


def _format_duration(months: int) -> str:
    years, rem_months = divmod(months, 12)
    parts = []
    if years:
        parts.append(f"{years} yr{'s' if years != 1 else ''}")
    if rem_months or not years:
        parts.append(f"{rem_months} mo{'s' if rem_months != 1 else ''}")
    return " ".join(parts)


def _month_index(value: datetime) -> int:
    return value.year * 12 + value.month - 1


def _entry_month_range(entry: dict) -> tuple[int, int] | None:
    """
    (first, last) month index of one work_experience entry, both months
    included: "Jan 2026 - Jun 2026" is 6 months, and an ongoing role
    ("Present", is_current, or no end date) runs to the current month.
    Resumes write dates however the candidate/parser phrased them ("Jan
    2021", "2021-01", "Present"), so parsing is fuzzy/best-effort and returns
    None rather than raising when a date can't be read.
    """
    start = _parse_month(entry.get("start_date"))
    if start is None:
        return None

    end_date_raw = (entry.get("end_date") or "").strip().lower()
    if entry.get("is_current") or end_date_raw in _CURRENT_MARKERS or not end_date_raw:
        end = datetime.now()
    else:
        end = _parse_month(entry.get("end_date"))
    if end is None:
        return None

    first, last = _month_index(start), _month_index(end)
    if last < first:
        return None
    return first, last


def _compute_entry_months(entry: dict) -> int | None:
    """Best-effort tenure (in months, both end months included) for one work_experience entry."""
    span = _entry_month_range(entry)
    return None if span is None else span[1] - span[0] + 1


def _months(entries: list[dict]) -> set[int]:
    """The calendar months these entries cover - overlapping (concurrent) roles count once."""
    covered: set[int] = set()
    for entry in entries:
        span = _entry_month_range(entry)
        if span is not None:
            covered.update(range(span[0], span[1] + 1))
    return covered


def _professional(entries: list[dict]) -> list[dict]:
    return [e for e in entries if not e.get("is_internship") and not e.get("is_volunteer")]


def _internships(entries: list[dict]) -> list[dict]:
    return [e for e in entries if e.get("is_internship") and not e.get("is_volunteer")]


def annotate_work_experience_durations(parsed_json: dict | None) -> dict | None:
    """
    Returns a copy of parsed_json with a computed duration attached to each
    work_experience entry, and total_experience_years recomputed from those
    same entries (professional employment only — internships and volunteer
    entries excluded) rather than trusting the AI-extracted figure verbatim,
    which is often just whatever the resume's own text claimed and can drift
    from what the listed start/end dates actually add up to. Response-time-
    only — never persisted, doesn't touch the stored parsed_json or the
    extraction schema/pipeline.
    """
    if not parsed_json or not parsed_json.get("work_experience"):
        return parsed_json

    annotated = dict(parsed_json)
    entry_months = [_compute_entry_months(entry) for entry in parsed_json["work_experience"]]
    annotated["work_experience"] = [
        {**entry, "duration_text": _format_duration(months) if months is not None else None}
        for entry, months in zip(parsed_json["work_experience"], entry_months)
    ]

    # Professional months covered, concurrent roles counted once.
    professional_months = _months(_professional(parsed_json["work_experience"]))
    if professional_months:
        annotated["total_experience_years"] = round(len(professional_months) / 12, 1)

    return annotated


def experience_years_for_scoring(parsed_json: dict | None, internship_credit: float = 0.0) -> float | None:
    """
    The candidate's counted experience for the minimum-experience check.

    Professional months covered (both end months included; concurrent roles
    counted once), plus internship months that don't overlap a professional
    role times internship_credit (the JD's setting: 1.0, 0.5 or 0.0 - see
    jd_calibration_rules.jd_internship_credit). Volunteer roles never count.
    Dates are preferred over the AI's figure. "Nothing counted" is 0 years
    rather than missing: a fresher with no roles, or only uncredited
    internships / volunteer roles, has 0 years against the minimum - a
    near-miss or a shortfall, not "not applicable".

    Still None (data missing - never held against the candidate) only when
    a professional role is listed but its dates can't be read, and no
    professional months were counted: the experience exists, the parse just
    couldn't measure it.
    """
    parsed = parsed_json or {}
    entries = parsed.get("work_experience") or []
    if not entries:
        stated = parsed.get("total_experience_years")
        return float(stated) if stated is not None else 0.0

    professional = _professional(entries)
    professional_months = _months(professional)
    if not professional_months and any(_entry_month_range(e) is None for e in professional):
        return None
    internship_months = _months(_internships(entries)) - professional_months
    counted = len(professional_months) + float(internship_credit) * len(internship_months)
    return round(counted / 12, 1)
