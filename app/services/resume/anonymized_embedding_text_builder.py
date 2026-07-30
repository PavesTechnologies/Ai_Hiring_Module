"""
M08-E01 T01/T02: builds the anonymised text EMBED_RESUME embeds, and a
lightweight verification pass over it.

Operates directly on resumes.parsed_json (a plain dict) rather than the
in-memory ResumeExtractionResponse object the synchronous resume
pipeline's own resume_embedding_text_builder.py uses - EMBED_RESUME runs
as its own decoupled Celery task with only the already-persisted JSON
available, not a live extraction object from a still-running pipeline
run. Also deliberately narrower in scope than that builder: this one
reads ONLY skills / work_experience (title, company, start_date,
end_date) / education (degree, field, institution) - never
certifications, summary, or total_experience_years.

Never reads name/email/phone/address/DOB/LinkedIn/GitHub/portfolio/
personal identifiers - none of those keys exist anywhere on parsed_json's
schema (ResumeExtractionResponse is PII-free by construction; see that
schema's own docstring). verify_anonymized_text() is a defensive regex
safety net on top of that structural guarantee, not a replacement for it.
"""

import re

_EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_URL_PATTERN = re.compile(r"https?://\S+|www\.\S+|linkedin\.com\S*|github\.com\S*", re.IGNORECASE)
# Deliberately conservative (10+ total chars: 1 leading digit + 8+ digit/
# dash/space/paren + 1 trailing digit) so a plain year range like
# "2019-2022" (9 chars) never false-positives - only letters break the
# digit/dash/space/paren run, so month-name dates ("Jan 2019") are safe too.
_PHONE_PATTERN = re.compile(r"(?<!\d)(\+?\d[\d\-\s()]{8,}\d)(?!\d)")


def build_anonymized_embedding_text(parsed_json: dict) -> str:
    parts: list[str] = []

    skills = parsed_json.get("skills") or []
    clean_skills = [str(skill) for skill in skills if skill]
    if clean_skills:
        parts.append("Skills: " + ", ".join(clean_skills))

    for entry in parsed_json.get("work_experience") or []:
        if not isinstance(entry, dict):
            continue
        header_bits = []
        if entry.get("title"):
            header_bits.append(str(entry["title"]))
        if entry.get("company"):
            header_bits.append(f"at {entry['company']}")
        header = " ".join(header_bits)

        dates = [str(d) for d in (entry.get("start_date"), entry.get("end_date")) if d]
        date_range = "-".join(dates) if dates else None

        segment = header
        if date_range:
            segment = f"{segment} ({date_range})" if segment else date_range
        if segment:
            parts.append("Experience: " + segment)

    for entry in parsed_json.get("education") or []:
        if not isinstance(entry, dict):
            continue
        education_bits = [
            str(entry[key]) for key in ("degree", "field", "institution") if entry.get(key)
        ]
        if education_bits:
            parts.append("Education: " + " ".join(education_bits))

    return "\n".join(parts)


def verify_anonymized_text(text: str) -> tuple[bool, str | None]:
    """Returns (is_valid, failure_reason) - failure_reason is None when is_valid is True."""
    if _EMAIL_PATTERN.search(text):
        return False, "Anonymised text appears to contain an email address."
    if _URL_PATTERN.search(text):
        return False, "Anonymised text appears to contain a URL or social-profile link."
    if _PHONE_PATTERN.search(text):
        return False, "Anonymised text appears to contain a phone number."
    return True, None
