"""
Scoring v2: deterministic catalogue scan of resume text.

Finds catalogue skills (canonical names and aliases) written in the resume
itself, whole-word, so a competency line the LLM leaves out of `skills`
still counts. Same text in, same skills out - no model involved.

Precision rules, in order:
  1. skill_scan_terms policy for the term (EXCLUDE / REQUIRE_CUE / ALLOW).
  2. Terms of 2 or fewer letters/digits are not scanned (AI, PM, R, CV,
     ES, ...) - too ambiguous without context; the LLM still reports them.
  3. A term naming more than one catalogue skill (IAM) is not scanned.
  4. Multi-word terms match anywhere, case-insensitively.
  5. An all-caps single word (AWS, SPM) matches anywhere, case-sensitively.
  6. Other single words that can't be an English word (C++, PostgreSQL,
     K8s) match anywhere, case-insensitively.
  7. Plain single words (Spark, Express, Testing) match only as a whole
     list item ("Python, Spark, SQL") - in a sentence they are too often
     ordinary English.
Longest match wins: "AI Governance" in the text is AI Governance, never
also Governance.

Distributed parts: list segments are run through split_compound_skill, so
"Change, Incident & Problem Management" yields Change Management even
though that exact text never appears. A part only counts on an exact
(normalised) catalogue name/alias, and is labelled DERIVED.
"""
import re
from dataclasses import dataclass, field
from uuid import UUID

from app.services.skills.compound_skill_splitter import comma_items, skill_list_segments, split_compound_skill
from app.services.skills.spelling import normalise_word

# Bump when scan/split/sense/union behaviour changes: it is part of the
# parse and confirmation cache keys and stored on every evidence row.
DETECTION_VERSION = "2026.10.3"

POLICY_EXCLUDE = "EXCLUDE"
POLICY_REQUIRE_CUE = "REQUIRE_CUE"
POLICY_ALLOW = "ALLOW"

MODE_ANYWHERE = "ANYWHERE"
MODE_LIST_ITEM = "LIST_ITEM"

ORIGIN_SCAN = "SCAN"
ORIGIN_DERIVED = "DERIVED"

_MIN_TERM_CHARS = 3
_MAX_TERM_TOKENS = 8
_TOKEN = re.compile(r"[A-Za-z0-9+#&]+")
_SQUASH = re.compile(r"[^a-z0-9+#&]+")
_VERSION_SUFFIX = re.compile(r"\s+v?\d+(\.\d+)*$", re.IGNORECASE)


def term_key(text: str) -> str:
    """
    Lower-cased, punctuation-free, American-spelling form used to compare
    terms and text ("Node.js" -> "node js", "Programme Management" ->
    "program management").
    """
    return " ".join(normalise_word(word) for word in _SQUASH.sub(" ", (text or "").lower()).split())


def squash_words(text: str) -> str:
    return f" {term_key(text)} "


@dataclass(frozen=True)
class ScanTermRule:
    policy: str
    cue_words: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Entry:
    skill_id: UUID
    term: str
    mode: str
    case_sensitive: bool
    cue_words: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScanHit:
    skill_id: UUID
    term: str
    origin: str          # SCAN or DERIVED
    line_no: int
    line: str
    # For DERIVED: the list segment it was distributed from.
    from_phrase: str | None = None
    # For DERIVED pattern-rule hits: which rule produced it.
    note: str | None = None


@dataclass
class ScanIndex:
    entries: dict[str, list[_Entry]] = field(default_factory=dict)
    max_tokens: int = 1

    @classmethod
    def build(cls, catalog: list, rules: dict[str, ScanTermRule] | None = None) -> "ScanIndex":
        """catalog: objects with .id/.canonical_name/.aliases (load_cached_active_skills)."""
        rules = rules or {}
        by_key: dict[str, dict[UUID, str]] = {}
        for skill in catalog:
            for term in [skill.canonical_name, *(skill.aliases or [])]:
                key = term_key(term)
                if key:
                    by_key.setdefault(key, {}).setdefault(skill.id, term)

        index = cls()
        for key, skills in by_key.items():
            if len(skills) != 1:
                continue  # rule 3: ambiguous across skills
            skill_id, term = next(iter(skills.items()))
            entry = _classify(skill_id, term, key, rules.get(key))
            if entry is None:
                continue
            index.entries.setdefault(key, []).append(entry)
            index.max_tokens = max(index.max_tokens, min(len(key.split()), _MAX_TERM_TOKENS))
        return index

    def lookup(self, key: str) -> _Entry | None:
        found = self.entries.get(key)
        return found[0] if found else None


def _classify(skill_id: UUID, term: str, key: str, rule: ScanTermRule | None) -> _Entry | None:
    tokens = key.split()
    if rule is not None:
        if rule.policy == POLICY_EXCLUDE:
            return None
        # A rule-listed single word matches only in the catalogue's casing
        # ("Go" next to a cue, never the verb "go").
        acronym = len(tokens) == 1
        if rule.policy == POLICY_REQUIRE_CUE:
            return _Entry(skill_id, term, MODE_ANYWHERE, acronym, tuple(rule.cue_words))
        if rule.policy == POLICY_ALLOW:
            return _Entry(skill_id, term, MODE_ANYWHERE, acronym)
    if sum(ch.isalnum() for ch in key) < _MIN_TERM_CHARS:
        return None                                                     # rule 2
    if len(tokens) >= 2:
        return _Entry(skill_id, term, MODE_ANYWHERE, False)             # rule 4
    if term.isupper() and any(ch.isalpha() for ch in term):
        return _Entry(skill_id, term, MODE_ANYWHERE, True)              # rule 5
    if not term.isalpha() or any(ch.isupper() for ch in term[1:]):
        return _Entry(skill_id, term, MODE_ANYWHERE, False)             # rule 6
    return _Entry(skill_id, term, MODE_LIST_ITEM, False)                # rule 7


def _cue_near(cue_words: tuple[str, ...], lines: list[str], line_no: int, own_key: str) -> bool:
    window = squash_words(" ".join(lines[max(0, line_no - 1): line_no + 2]))
    return any(f" {term_key(cue)} " in window for cue in cue_words if term_key(cue) and term_key(cue) != own_key)


def _case_ok(entry: _Entry, original: str) -> bool:
    if not entry.case_sensitive:
        return True
    return " ".join(_TOKEN.findall(original)) == " ".join(_TOKEN.findall(entry.term))


def scan_text(text: str, index: ScanIndex) -> list[ScanHit]:
    """Every catalogue skill found in the text - literal (SCAN) and distributed (DERIVED) - first occurrence per line."""
    lines = (text or "").splitlines()
    hits: list[ScanHit] = []
    seen: set[tuple[UUID, int]] = set()

    # Literal, ANYWHERE terms: greedy longest match per line.
    for line_no, line in enumerate(lines):
        tokens = list(_TOKEN.finditer(line))
        i = 0
        while i < len(tokens):
            matched = 0
            for n in range(min(index.max_tokens, len(tokens) - i), 0, -1):
                window = tokens[i:i + n]
                entry = index.lookup(" ".join(normalise_word(t.group(0).lower()) for t in window))
                if entry is None or entry.mode != MODE_ANYWHERE:
                    continue
                original = line[window[0].start():window[-1].end()]
                if not _case_ok(entry, original):
                    continue
                if entry.cue_words and not _cue_near(entry.cue_words, lines, line_no, term_key(entry.term)):
                    continue
                if (entry.skill_id, line_no) not in seen:
                    seen.add((entry.skill_id, line_no))
                    hits.append(ScanHit(entry.skill_id, entry.term, ORIGIN_SCAN, line_no, line))
                matched = n
                break
            i += matched or 1

    # Whole list items (rule 7) and distributed parts.
    for line_no, segment in skill_list_segments(text):
        for item in comma_items(segment):
            entry = index.lookup(term_key(_VERSION_SUFFIX.sub("", item)))
            if entry is not None and entry.mode == MODE_LIST_ITEM and (entry.skill_id, line_no) not in seen:
                seen.add((entry.skill_id, line_no))
                hits.append(ScanHit(entry.skill_id, entry.term, ORIGIN_SCAN, line_no, lines[line_no]))
        for part in split_compound_skill(segment):
            entry = index.lookup(term_key(part))
            if entry is None or (entry.skill_id, line_no) in seen:
                continue
            if entry.cue_words and not _cue_near(entry.cue_words, lines, line_no, term_key(entry.term)):
                continue
            seen.add((entry.skill_id, line_no))
            hits.append(ScanHit(entry.skill_id, entry.term, ORIGIN_DERIVED, line_no, lines[line_no], segment))
    return hits


@dataclass(frozen=True)
class PatternRule:
    skill_id: UUID
    name: str
    anchor_terms: tuple[str, ...]
    near_terms: tuple[str, ...]
    max_gap_words: int
    sections: frozenset[str]
    anchor_case_sensitive: bool = True
    # LINE: resume text lines in `sections`; JOB_TITLE: parsed job titles.
    match_scope: str = "LINE"


def _find_terms(tokens: list[str], terms: tuple[str, ...], case_sensitive: bool) -> list[tuple[int, int]]:
    """(start, end) token spans of every occurrence of any term."""
    spans = []
    for term in terms:
        wanted = _TOKEN.findall(term)
        if not wanted:
            continue
        if not case_sensitive:
            wanted = [normalise_word(w.lower()) for w in wanted]
        for i in range(len(tokens) - len(wanted) + 1):
            window = tokens[i:i + len(wanted)]
            if not case_sensitive:
                window = [normalise_word(w.lower()) for w in window]
            if window == wanted:
                spans.append((i, i + len(wanted)))
    return spans


def pattern_hits(lines: list[str], line_sections: list[str], rules: list[PatternRule]) -> list[ScanHit]:
    """
    DERIVED hits for wording patterns (skill_pattern_rules): an anchor term
    within max_gap_words of a near term, on a line in one of the rule's
    sections - e.g. "led global AI program governance" -> AI Governance.
    """
    hits = []
    for rule in rules:
        if rule.match_scope != "LINE":
            continue
        for line_no, line in enumerate(lines):
            if line_sections[line_no] not in rule.sections:
                continue
            tokens = _TOKEN.findall(line)
            anchors = _find_terms(tokens, rule.anchor_terms, rule.anchor_case_sensitive)
            nears = _find_terms(tokens, rule.near_terms, False)
            if any(min(abs(n_start - a_end), abs(a_start - n_end)) <= rule.max_gap_words
                   for a_start, a_end in anchors for n_start, n_end in nears):
                hits.append(ScanHit(rule.skill_id, rule.name, ORIGIN_DERIVED, line_no, line,
                                    note=f"pattern rule '{rule.name}'"))
    return hits


def _pattern_matches(rule: PatternRule, text: str) -> bool:
    tokens = _TOKEN.findall(text)
    anchors = _find_terms(tokens, rule.anchor_terms, rule.anchor_case_sensitive)
    nears = _find_terms(tokens, rule.near_terms, False)
    return any(min(abs(n_start - a_end), abs(a_start - n_end)) <= rule.max_gap_words
               for a_start, a_end in anchors for n_start, n_end in nears)


@dataclass(frozen=True)
class TitleHit:
    skill_id: UUID
    rule_name: str
    title: str


def title_pattern_hits(job_titles: list[str], rules: list[PatternRule]) -> list[TitleHit]:
    """
    DERIVED hits from JOB_TITLE pattern rules over the parsed work-experience
    titles - e.g. "Senior Programme Manager, Advisory" -> Program Management.
    Titles only: a "Target roles: ... Programme Head" line is not a title held.
    """
    hits = []
    for rule in rules:
        if rule.match_scope != "JOB_TITLE":
            continue
        for title in job_titles:
            if title and _pattern_matches(rule, title):
                hits.append(TitleHit(rule.skill_id, rule.name, title))
    return hits
