from datetime import datetime

from dateutil import parser as date_parser
from dateutil.relativedelta import relativedelta

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


def compute_entry_duration(entry: dict) -> dict | None:
    """
    Best-effort tenure for one work_experience entry, from its free-form
    start_date/end_date strings — resumes write these however the
    candidate/parser phrased them ("Jan 2021", "2021-01", "Present"), so
    parsing is fuzzy/best-effort and returns None rather than raising when a
    date can't be read.
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

    delta = relativedelta(end, start)
    total_months = delta.years * 12 + delta.months
    if total_months < 0:
        return None

    return {
        "duration_years": round(total_months / 12, 1),
        "duration_text": _format_duration(total_months),
    }


def annotate_work_experience_durations(parsed_json: dict | None) -> dict | None:
    """
    Returns a copy of parsed_json with a computed duration attached to each
    work_experience entry — response-time-only annotation, never persisted,
    doesn't touch the stored parsed_json or the extraction schema/pipeline.
    """
    if not parsed_json or not parsed_json.get("work_experience"):
        return parsed_json

    annotated = dict(parsed_json)
    annotated["work_experience"] = [
        {**entry, **(compute_entry_duration(entry) or {"duration_years": None, "duration_text": None})}
        for entry in parsed_json["work_experience"]
    ]
    return annotated
