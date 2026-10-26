"""
A core skill's requirement text, suggested from the JD's own wording: the JD line that
names the skill (or one of its aliases), preferring a bullet under a must-have /
requirements heading - "Data Engineering (batch and streaming pipelines, lakehouse or
warehouse design)". HR edits it in calibration; the paraphrase search and the voters use
it to see what this JD means by the skill.
"""
import re

MAX_LENGTH = 500
_REQUIREMENT_HEADING = re.compile(r"\b(must[- ]haves?|required|requirements|qualifications|essential|key skills)\b", re.I)
_OTHER_HEADING = re.compile(r"\b(good to have|nice to have|preferred|desirable|bonus|about|responsibilities|"
                            r"benefits|education|experience)\b", re.I)
# A heading starts with a heading word ("Must-have skills", "Good to have", "Skills") or ends
# with a colon; a short line without a bullet marker ("Cloud Architecture (AWS or Azure)",
# "Databricks") is a list item, not a heading.
_HEADING_START = re.compile(r"^\W*(must[- ]haves?|required|requirements|qualifications|essential|key|skills|"
                            r"good to have|nice to have|preferred|desirable|bonus|about|responsibilities|"
                            r"benefits|education|experience|what you)\b", re.I)
_BULLET = re.compile(r"^\s*(?:[-*•·–]|\d+[.)])\s+")


def _is_heading(line: str) -> bool:
    text = line.strip()
    if not text or len(text.rstrip(":").split()) > 6 or _BULLET.match(line) or text.endswith("."):
        return False
    return text.endswith(":") or bool(_HEADING_START.match(text))


def _mentions(line: str, terms: list[str]) -> bool:
    for term in terms:
        if not term:
            continue
        # a short all-capitals alias ("ML") only in capitals; anything else case-insensitive
        flags = 0 if (len(term) <= 3 and term.isupper()) else re.I
        if re.search(rf"(?<![\w]){re.escape(term)}(?![\w])", line, flags):
            return True
    return False


def suggest_requirement_text(raw_text: str | None, name: str, aliases=()) -> str | None:
    """The JD's own line about this skill, or None when the JD never names it."""
    terms = [name, *[a for a in (aliases or ()) if a]]
    best, best_rank = None, None
    section = None                                   # "required" / "other" / None, by the heading above
    for position, raw in enumerate((raw_text or "").splitlines()):
        line = raw.strip()
        if not line:
            continue
        mentioned = _mentions(line, terms)
        if not mentioned and _is_heading(line):        # a line naming the skill is never taken as a heading
            # "Preferred qualifications" is not a must-have; "Skills" alone or "Tech stack:" says neither.
            section = ("other" if _OTHER_HEADING.search(line)
                       else "required" if _REQUIREMENT_HEADING.search(line) else None)
            continue
        if not mentioned:
            continue
        bullet = bool(_BULLET.match(raw))
        rank = (0 if section == "required" else 1 if section is None else 2, 0 if bullet else 1, position)
        if best_rank is None or rank < best_rank:
            best, best_rank = _BULLET.sub("", raw).strip(), rank
    return best[:MAX_LENGTH] if best else None
