"""
Which resume section a line is in - the nearest known heading above it.

Used by scoring v2's core confirmation: an exact scan match in a strong
section (summary, experience, skills) is auto-confirmed; one in a weak
section (recognition, interests, events, ...) goes to the LLM, and every
excerpt sent shows its section so the model can tell an award title from a
skill.

Headings are matched against a fixed vocabulary (not "any all-caps line",
which also catches names and product lines like "RHEL 9"), including the
letter-spaced style "R E C O G N I T I O N". Lines before the first heading
are the resume's header/summary.
"""
import re

SECTION_SUMMARY = "SUMMARY"
SECTION_EXPERIENCE = "EXPERIENCE"
SECTION_SKILLS = "SKILLS"
SECTION_EDUCATION = "EDUCATION"
SECTION_CERTIFICATIONS = "CERTIFICATIONS"
SECTION_PROJECTS = "PROJECTS"
SECTION_RECOGNITION = "RECOGNITION"
SECTION_INTERESTS = "INTERESTS"
SECTION_EVENTS = "EVENTS"
SECTION_OTHER = "OTHER"

# Auto-confirm is allowed only in these.
STRONG_SECTIONS = frozenset({SECTION_SUMMARY, SECTION_EXPERIENCE, SECTION_SKILLS})
# Always confirmed by the LLM, never auto.
WEAK_SECTIONS = frozenset({SECTION_RECOGNITION, SECTION_INTERESTS, SECTION_EVENTS})

_VOCABULARY = [
    (SECTION_SUMMARY, r"(professional |executive |career |personal )?(summary|profile|objective|overview|about me)"),
    (SECTION_EXPERIENCE, r"(work |professional |relevant |career |employment )?(experience|history)|employment"
                         r"|career highlights|key achievements in roles"),
    (SECTION_SKILLS, r"(key |core |technical |professional |functional |leadership )?(skills|competencies|competences"
                     r"|capabilities|strengths)( & tools| and tools)?|areas of expertise|expertise|specialisations?"
                     r"|specializations?|tools( & technologies)?|technologies"),
    (SECTION_EDUCATION, r"education|academic( background| qualifications)?|qualifications"),
    (SECTION_CERTIFICATIONS, r"certifications?|licen[cs]es( & certifications)?|credentials|training( & certifications)?"
                             r"|courses"),
    (SECTION_PROJECTS, r"(key |selected )?projects"),
    (SECTION_RECOGNITION, r"recognitions?|awards?( & recognitions?| and recognitions?)?|honou?rs|achievements( & awards)?"
                          r"|accolades"),
    (SECTION_INTERESTS, r"interests|hobbies|personal interests|extra[- ]?curricular( activities)?"),
    (SECTION_EVENTS, r"events|speaking( engagements)?|talks|conferences|publications|community( involvement)?"
                     r"|memberships|affiliations|volunteer(ing)?"),
]
_PATTERNS = [(name, re.compile(rf"^\W*(?:{pattern})\W*$", re.IGNORECASE)) for name, pattern in _VOCABULARY]
_SPACED = re.compile(r"^(?:\w ){3,}\w$")
_MAX_HEADING_CHARS = 60


def heading_section(line: str) -> str | None:
    """The section a heading line opens, or None when the line isn't a heading."""
    text = (line or "").strip()
    if not text or len(text) > _MAX_HEADING_CHARS:
        return None
    if _SPACED.match(text):
        text = text.replace(" ", "")
    for name, pattern in _PATTERNS:
        if pattern.match(text):
            return name
    return None


def section_of_line(lines: list[str], line_no: int) -> str:
    """Section containing lines[line_no] (a heading line belongs to the section it opens)."""
    for index in range(min(line_no, len(lines) - 1), -1, -1):
        section = heading_section(lines[index])
        if section:
            return section
    return SECTION_SUMMARY


def heading_text_of_line(lines: list[str], line_no: int) -> str | None:
    """The heading text itself (e.g. "R E C O G N I T I O N"), for showing the LLM the excerpt's context."""
    for index in range(min(line_no, len(lines) - 1), -1, -1):
        if heading_section(lines[index]):
            return lines[index].strip()
    return None
