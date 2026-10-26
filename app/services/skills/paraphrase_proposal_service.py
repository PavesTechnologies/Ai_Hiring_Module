"""
Scoring v2 paraphrase proposals (Phase 3 Step 6).

For a JD core skill still NOT_FOUND or UNCERTAIN after the scan,
certification mapping and senses, compare the skill - name, aliases, short
description, and for an ambiguous skill the JD sense's description - with the
resume's experience lines by embedding similarity (the active local model,
no LLM). At most the top max_lines lines at or above the threshold are
proposed; the caller sends them to the 3-vote core confirmation as DERIVED
evidence (never auto-confirmed).

A line already quoted as evidence for another JD core skill isn't proposed.
Among the skills searched, a line may be proposed for several of them (at
most max_lines per skill); each skill's vote decides.

A line is never proposed when:
  - it is already quoted as evidence for that skill;
  - it holds a term blocklisted for that skill (SCORING_V2_MATCH_BLOCKLIST,
    e.g. "IT governance" for AI Governance) - paraphrase can't route around
    the blocklist;
  - its own cue words definitely put it in another sense than the JD's
    (organisational change lines are never proposed for ITSM change).
"""
import hashlib
import re
from dataclasses import dataclass

from app.services.skills.resume_sections import section_of_line
from app.services.skills.resume_skill_scanner import squash_words, term_key

DEFAULT_PARAPHRASE_CONFIG = {
    "enabled": True,
    "threshold": 0.45,
    "max_lines": 3,
    "sections": ["EXPERIENCE"],
    "min_words": 4,
    "max_words": 40,
}
_DEFINITE_JD_SENSE = {"cue", "title", "hr"}
_SPACES = re.compile(r"\s+")

_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE_RANGE = re.compile(
    rf"\b(?:{_MONTH}\s+)?\d{{4}}\s*[-–—]\s*(?:present|current|now|today|(?:{_MONTH}\s+)?\d{{4}})\b", re.I)
_HEADER_MARKS = ("—", "·", "|")
_AFTER_TITLE = ("—", "–", "-", "·", "|", ",", "@", "(", "at ")


def is_title_line(line: str, job_titles=()) -> bool:
    """
    A job-title line or a role header - never paraphrase evidence (a title says what the
    job was called, not what was done). Only the whole line: a bullet that mentions a
    title word ("Led the Data Engineering team that built...") is not one.
      - a parsed job title on its own, or followed by the employer / dates
        ("Head of Data Engineering — Orbit Payments · Oct 2021 – present");
      - a role header: a date range and a header mark (— · |), with no work verb.
    The explicit JOB_TITLE pattern rules are separate and unaffected.
    """
    from app.services.skills.evidence_rules import DEFAULT_RULES
    text = _SPACES.sub(" ", line or "").strip(" \t•·-–")
    low = text.lower()
    for title in job_titles or ():
        t = _SPACES.sub(" ", str(title)).strip().lower()
        if t and low.startswith(t):
            rest = low[len(t):].strip()
            if not rest or rest.startswith(_AFTER_TITLE):
                return True
    return (bool(_DATE_RANGE.search(text)) and any(mark in text for mark in _HEADER_MARKS)
            and not DEFAULT_RULES.does_the_work(text))


@dataclass(frozen=True)
class ParaphraseTarget:
    canonical_skill_id: object
    name: str
    aliases: tuple[str, ...] = ()
    description: str | None = None
    sense: str | None = None
    sense_basis: str | None = None
    sense_description: str | None = None
    # What this JD asks for under the skill (jd_skills.requirement_text), when set.
    requirement_text: str | None = None


@dataclass(frozen=True)
class Proposal:
    canonical_skill_id: object
    line: str
    line_no: int
    similarity: float
    section: str


def skill_text(target: ParaphraseTarget) -> str:
    parts = [target.name]
    if target.aliases:
        parts.append("Also: " + ", ".join(target.aliases))
    if target.description:
        parts.append(target.description)
    if target.sense_description:
        parts.append(target.sense_description)
    if target.requirement_text:
        parts.append(target.requirement_text)
    return ". ".join(p.strip().rstrip(".") for p in parts if p and p.strip()) + "."


def candidate_lines(text: str, config: dict, exclude_line=None, job_titles=()) -> list[tuple[int, str, str]]:
    """
    (line index, line, section) of the resume lines paraphrase may propose.
    exclude_line(line) -> True: never proposed (a qualifier line, "currently learning ...").
    job_titles: the parse's work-experience titles - a title line or role header is never proposed.
    """
    lines = (text or "").splitlines()
    sections = set(config.get("sections") or ["EXPERIENCE"])
    out = []
    for i, raw in enumerate(lines):
        line = _SPACES.sub(" ", raw).strip(" \t•·-–")
        words = len(line.split())
        if words < int(config.get("min_words", 4)) or words > int(config.get("max_words", 40)):
            continue
        section = section_of_line(lines, i)
        if section in sections and not (exclude_line and exclude_line(raw)) and not is_title_line(line, job_titles):
            out.append((i, line, section))
    return out


def proposals_hash(proposals: list[Proposal]) -> str:
    raw = "|".join(f"{p.canonical_skill_id}:{p.line}" for p in sorted(proposals, key=lambda p: (str(p.canonical_skill_id), p.line)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class ParaphraseProposalService:
    def __init__(self, embed, resolver, blocked: frozenset = frozenset()):
        """embed: list[str] -> list[list[float]] (normalised vectors)."""
        self.embed = embed
        self.resolver = resolver
        self.blocked = blocked

    def propose(self, text: str, targets: list[ParaphraseTarget], evidence_rows: list, config: dict | None = None,
                core_skill_ids=None, exclude_line=None, job_titles=()) -> tuple[list[Proposal], dict]:
        """
        (proposals, scores) - scores[skill_id] lists every candidate line's
        similarity (and why a line was excluded), for the dry run.
        core_skill_ids: every JD core skill; a line quoted as evidence for any
        of them (other than the target) is never proposed.
        job_titles: the parse's work-experience titles (title lines are never proposed).
        """
        config = {**DEFAULT_PARAPHRASE_CONFIG, **(config or {})}
        lines = candidate_lines(text, config, exclude_line, job_titles)
        if not targets or not lines:
            return [], {}
        vectors = self.embed([skill_text(t) for t in targets] + [line for _, line, _ in lines])
        skill_vectors, line_vectors = vectors[:len(targets)], vectors[len(targets):]
        quoted = _quoted_lines(evidence_rows)
        core_ids = {str(i) for i in (core_skill_ids or [t.canonical_skill_id for t in targets])}
        threshold, max_lines = float(config["threshold"]), int(config["max_lines"])

        similarity = [[round(sum(a * b for a, b in zip(sv, lv)), 4) for lv in line_vectors] for sv in skill_vectors]
        reasons = [[self._excluded(t, line, quoted.get(str(t.canonical_skill_id), set()))
                    for _, line, _ in lines] for t in targets]
        for ti, target in enumerate(targets):
            for li, (_, line, _) in enumerate(lines):
                if reasons[ti][li] is None:
                    used = _quoted_for_other_core(line, quoted, core_ids, str(target.canonical_skill_id))
                    if used:
                        reasons[ti][li] = "evidence for another core skill"
        proposals, scores = [], {}
        for ti, target in enumerate(targets):
            scored = [(similarity[ti][li], line_no, line, section, reasons[ti][li])
                      for li, (line_no, line, section) in enumerate(lines)]
            scored.sort(key=lambda s: -s[0])
            scores[str(target.canonical_skill_id)] = [
                {"line": line, "similarity": sim, "excluded": reason} for sim, _, line, _, reason in scored]
            kept = [s for s in scored if s[4] is None and s[0] >= threshold][:max_lines]
            proposals += [Proposal(target.canonical_skill_id, line, line_no, sim, section)
                          for sim, line_no, line, section, _ in kept]
        return proposals, scores

    def _excluded(self, target: ParaphraseTarget, line: str, quoted: set[str]) -> str | None:
        squashed = squash_words(line)
        core = squashed.strip()
        if any(core in q or q.strip() in squashed for q in quoted if q.strip()):
            return "already evidence"
        for term, skill_id in self.blocked:
            if skill_id == target.canonical_skill_id and term and f" {term} " in f" {squashed} ":
                return "blocklisted term"
        if target.sense and target.sense_basis in _DEFINITE_JD_SENSE and self.resolver.has_senses(target.canonical_skill_id):
            choice = self.resolver.resolve_snippet(target.canonical_skill_id, line, target.name)
            if choice.resolved and choice.sense and choice.sense != target.sense:
                return f"sense {choice.sense}"
        return None


def _quoted_for_other_core(line: str, quoted: dict[str, set[str]], core_ids: set[str], target_id: str) -> bool:
    squashed = squash_words(line)
    core = squashed.strip()
    for skill_id in core_ids - {target_id}:
        if any(core in q or q.strip() in squashed for q in quoted.get(skill_id, ()) if q.strip()):
            return True
    return False


def _quoted_lines(evidence_rows: list) -> dict[str, set[str]]:
    quoted: dict[str, set[str]] = {}
    for row in evidence_rows or []:
        skill_id = row.get("canonical_skill_id") if isinstance(row, dict) else getattr(row, "canonical_skill_id", None)
        items = row.get("evidence") if isinstance(row, dict) else getattr(row, "evidence", None)
        for item in items or []:
            text = (item or {}).get("text")
            if text:
                quoted.setdefault(str(skill_id), set()).add(squash_words(text))
    return quoted


__all__ = ["ParaphraseProposalService", "ParaphraseTarget", "Proposal", "candidate_lines", "proposals_hash",
           "skill_text", "term_key", "DEFAULT_PARAPHRASE_CONFIG"]
