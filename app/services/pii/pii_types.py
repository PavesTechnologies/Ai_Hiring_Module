import enum
from dataclasses import dataclass


class PIIType(str, enum.Enum):
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    LINKEDIN = "LINKEDIN"
    GITHUB = "GITHUB"
    PORTFOLIO = "PORTFOLIO"


@dataclass(frozen=True)
class PIIFinding:
    """
    One detected PII occurrence. start/end are character offsets into the
    text that was scanned (context.cleaned_text) — redaction replaces
    text[start:end] with a placeholder. matched_text is kept only for the
    lifetime of the in-memory pipeline run (e.g. bulk upload reads it to
    resolve Candidate.email/phone); it is never logged or persisted raw.
    """

    pii_type: PIIType
    start: int
    end: int
    matched_text: str
