"""
Splits compound competency phrases from resumes into the individual skills
they name, e.g.

    "IT Project & Program Management"         -> IT Project Management, Program Management
    "Change, Incident & Problem Management"   -> Change Management, Incident Management, Problem Management
    "AI & Digital Transformation"             -> AI Transformation, Digital Transformation

Resumes (especially senior, non-engineering ones) list competencies this
way, and matching the whole phrase against the skill catalog finds none of
the parts - a candidate listing "IT Project & Program Management" was
scored as missing Program Management.

Only splits on ",", a spaced "&" and a spaced "and" - never on "/" or "-", which
join a single term far more often than two ("CI/CD", "AI/ML", "TCP/IP",
"Front-end"). When the last part ends in a shared head noun ("Management",
"Transformation", ...) that earlier parts lack, the head is carried over to
them. A single-word last part ("Team Building & Leadership") is a skill of
its own, so nothing is carried.

Pure and conservative: callers keep the original phrase and only use a
part when it matches a known skill (see ResumeProcessingPipeline), so an
odd split never creates noise.
"""
import re

# "&" only separates when spaced ("A & B") - unspaced it is part of one term
# (P&L, R&D, M&A, AT&T). Commas and a spaced "and" always separate.
_SEPARATORS = re.compile(r"\s*,\s*|\s+&\s+|\s+and\s+", re.IGNORECASE)
_MAX_PARTS = 6
_MAX_WORDS_PER_PART = 5
# Every part must start with a real word - never a lone letter/fragment.
_MIN_FIRST_WORD_LETTERS = 2


def split_compound_skill(phrase: str) -> list[str]:
    """The individual skills in a compound phrase, or [] when it isn't one."""
    text = (phrase or "").strip()
    if not _SEPARATORS.search(text):
        return []
    parts = [p.strip() for p in _SEPARATORS.split(text) if p and p.strip()]
    if len(parts) < 2 or len(parts) > _MAX_PARTS:
        return []
    if any(len(p.split()) > _MAX_WORDS_PER_PART for p in parts):
        return []
    if any(sum(ch.isalpha() for ch in p.split()[0]) < _MIN_FIRST_WORD_LETTERS for p in parts):
        return []

    last_words = parts[-1].split()
    if len(last_words) >= 2:
        head = last_words[-1]
        parts = [
            p if p.split()[-1].lower() == head.lower() else f"{p} {head}"
            for p in parts[:-1]
        ] + [parts[-1]]

    seen: set[str] = set()
    unique = []
    for part in parts:
        key = part.lower()
        if key != text.lower() and key not in seen:
            seen.add(key)
            unique.append(part)
    return unique if len(unique) >= 2 else []


# Raw-text segmentation for the v2 scan: resumes list competencies as
# lines, bullets or "|"/";"-separated rows ("Core Competencies: AI & Digital
# Transformation | Program Management"). Commas are left inside a segment -
# split_compound_skill distributes over them ("Change, Incident & Problem
# Management") and comma_items() lists them for single-word matching.
_SEGMENT_SEPARATORS = re.compile(r"\s*(?:\||;|•|·|▪|●|◦|– | - |:\s)\s*")
_MAX_SEGMENT_WORDS = 14


def skill_list_segments(text: str) -> list[tuple[int, str]]:
    """(line index, segment) for every short, list-like piece of the text."""
    segments = []
    for line_no, line in enumerate((text or "").splitlines()):
        for piece in _SEGMENT_SEPARATORS.split(line):
            piece = piece.strip(" \t.,-")
            if piece and len(piece.split()) <= _MAX_SEGMENT_WORDS:
                segments.append((line_no, piece))
    return segments


def comma_items(segment: str) -> list[str]:
    """The comma/and-separated items of a list segment, as written (no head-noun carry)."""
    return [p.strip() for p in _SEPARATORS.split(segment or "") if p and p.strip()]
