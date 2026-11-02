"""
The candidate's name, found before the resume parse so the parse never sees it.

A name can carry gender, religion, region or community, and the full parse was seen to keep
different skills for the same resume under different names. So the name is taken from the
first lines of the resume first - deterministically where the header has an ordinary name line,
otherwise by one small LLM call on just those lines - and the parse gets the text with the name
replaced (name_mask.NameMask). The parse's full_name is then set from here, not by the model.

Contact details are already redacted (PII redaction) before this runs.
"""
import logging
import re

from pydantic import BaseModel

from app.services.llm.call_log import llm_call_source
from app.services.skills.name_mask import PLACEHOLDER

logger = logging.getLogger(__name__)

HEADER_LINES = 8
_MAX_LINE = 60
_LABEL = re.compile(r"^\s*(?:full\s+)?name\s*[:\-–—]\s*", re.IGNORECASE)
_SPLIT = re.compile(r"\s+[|·•,–—-]\s+|\s{3,}|\t")
_BULLET = re.compile(r"^[\s#*\-•>]+")
# Words that make a header line something other than a person's name.
_NOT_NAME = {
    "resume", "résumé", "curriculum", "vitae", "cv", "biodata", "profile", "summary", "objective", "contact",
    "experience", "education", "skills", "projects", "certifications", "personal", "details", "information",
    "address", "email", "phone", "mobile", "linkedin", "github", "portfolio", "page", "career", "professional",
    "engineer", "developer", "manager", "consultant", "analyst", "architect", "lead", "senior", "junior",
    "intern", "director", "head", "officer", "specialist", "designer", "scientist", "administrator",
    "executive", "associate", "trainee", "fresher", "graduate", "student", "data", "software", "technologies",
    "solutions", "services", "limited", "ltd", "pvt", "inc", "india", "university", "college", "institute",
    # degrees, often written with an initial ("B. Tech", "M. Sc")
    "tech", "sc", "com", "ba", "ma", "mba", "bca", "mca", "phd", "be", "me", "arch", "pharm", "ed",
}

NAME_PROMPT = (
    "Below are the first lines of a resume. Return the candidate's own full name exactly as written there, "
    "or null if those lines don't contain a person's name. Never return a company, a job title, a section "
    "heading or a place."
)


class HeaderName(BaseModel):
    full_name: str | None = None


def header_lines(text: str | None, count: int = HEADER_LINES) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()][:count]


def _tokens_look_like_a_name(candidate: str) -> bool:
    if not candidate or len(candidate) > _MAX_LINE or any(ch.isdigit() for ch in candidate):
        return False
    if "[" in candidate or "@" in candidate or "/" in candidate:
        return False
    tokens = candidate.split()
    if not 2 <= len(tokens) <= 4:
        return False
    letters = 0
    for token in tokens:
        core = token.rstrip(".").replace("'", "").replace("’", "").replace("-", "")
        if not core or not core.isalpha() or not token[0].isupper():
            return False
        if core.casefold() in _NOT_NAME:
            return False
        letters += len(core)
    full_words = [t for t in tokens if len(t.rstrip(".")) >= 2]
    # two words, or initials with one word of 3+ letters ("R. K. Narayan", "K Srinivas")
    return letters >= 4 and (len(full_words) >= 2 or (len(full_words) == 1 and len(full_words[0]) >= 3))


def _clean(name: str) -> str:
    name = " ".join(name.split())
    return name.title() if name.isupper() else name


def detect_header_name(lines: list[str]) -> str | None:
    """The first header line (or its first part, before a separator) that reads as a person's name."""
    for line in lines:
        line = _BULLET.sub("", _LABEL.sub("", line)).strip()
        for part in [line, *_SPLIT.split(line)[:1]]:
            part = part.strip(" :;")
            if _tokens_look_like_a_name(part):
                return _clean(part)
    return None


def _normalised(value: str) -> str:
    return " ".join(value.casefold().split())


def llm_header_name(extraction_service, lines: list[str]) -> str | None:
    """One small call on the header lines only; the answer must appear in them, else None."""
    if not lines or extraction_service is None:
        return None
    header = "\n".join(lines)
    try:
        with llm_call_source("resume_name"):
            answer = extraction_service.generate_structured(f"{NAME_PROMPT}\n\nResume header:\n{header}", HeaderName)
    except Exception:
        logger.exception("Name lookup on the resume header failed - parsing without a name to hide.")
        return None
    name = (answer or {}).get("full_name") if isinstance(answer, dict) else None
    if not isinstance(name, str) or not name.strip() or PLACEHOLDER in name:
        return None
    if _normalised(name) not in _normalised(header):
        logger.warning("Name lookup returned a name that isn't in the header - ignored.")
        return None
    return _clean(name)


def find_candidate_name(text: str | None, extraction_service=None) -> tuple[str | None, str]:
    """(name as written in the resume or None, how it was found: "header", "llm" or "none")."""
    lines = header_lines(text)
    name = detect_header_name(lines)
    if name:
        return name, "header"
    name = llm_header_name(extraction_service, lines)
    return (name, "llm") if name else (None, "none")
