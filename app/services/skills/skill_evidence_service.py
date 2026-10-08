"""
Scoring v2: a resume's skill evidence - the union of the deterministic text
scan and the LLM extraction.

  SCAN      the catalogue name/alias is written in the resume (scanner)
  LLM       the LLM listed it in `skills` and it matched the catalogue
  DERIVED   distributed from a compound phrase ("Change, Incident &
            Problem Management" -> Change Management), by the scanner on
            the raw text or by the v1 splitter on an LLM phrase
  INFERRED  the LLM inferred it with a quote that was found in the resume

The LLM can add skills but never remove a scanned one: every scanned skill
is kept whatever the extraction says. Each occurrence gets a sense when the
skill has senses (skill_senses); one evidence row per (skill, sense).

Recognition (how sure the system is that the text names this skill):
SCAN and strong-tier LLM matches 1.0; DERIVED, INFERRED and fuzzy/semantic
LLM matches 0.8. Scoring v2 reads these from config; the values here are
the defaults stored with the evidence.

Pure: build_evidence() takes plain inputs so it can run in the pipeline,
in a backfill over stored resumes, and in tests alike.
"""
import hashlib
from dataclasses import dataclass, field
from uuid import UUID

from app.models.scoring_v2 import ResumeSkillEvidence
from app.services.skills.resume_skill_scanner import (
    DETECTION_VERSION, ORIGIN_DERIVED, ScanIndex, pattern_hits, scan_text, squash_words, term_key, title_pattern_hits,
)
from app.services.skills.resume_sections import (
    SECTION_EXPERIENCE, STRONG_SECTIONS, heading_section, heading_text_of_line, section_of_line,
)
from app.services.skills.skill_sense_resolver import SenseChoice, SkillSenseResolver

SOURCE_SCAN = "SCAN"
SOURCE_LLM = "LLM"
SOURCE_DERIVED = "DERIVED"
SOURCE_INFERRED = "INFERRED"

# Tie-break when two sources give the same recognition.
_SOURCE_ORDER = {SOURCE_SCAN: 0, SOURCE_LLM: 1, SOURCE_DERIVED: 2, SOURCE_INFERRED: 3}

_STRONG_TIERS = {"EXACT", "ALIAS", "CASE_INSENSITIVE", "RULE_BASED", "MANUAL_HR"}

DEFAULT_RECOGNITION = {
    SOURCE_SCAN: 1.0,
    SOURCE_LLM: 1.0,
    "LLM_WEAK": 0.8,        # LLM skill matched only on a fuzzy/semantic tier
    SOURCE_DERIVED: 0.8,
    SOURCE_INFERRED: 0.8,
}

_INFERRED_MARKER = " (inferred: "
_FROM_MARKER = ' (from "'
_SNIPPET_CHARS = 360
_MAX_SNIPPETS = 3


def _strong_scan(item: "_Occurrence", lines: list[str]) -> bool:
    return (item.source == SOURCE_SCAN and item.line_no is not None
            and section_of_line(lines, item.line_no) in STRONG_SECTIONS)


def text_hash(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LLMSkillMatch:
    """A v1 skill match (pipeline SkillMatchResult or stored candidate_skills row)."""
    canonical_skill_id: UUID
    raw_text: str
    match_tier: str


@dataclass(frozen=True)
class ParseInfo:
    provider: str | None = None
    model: str | None = None
    used_fallback: bool = False


@dataclass
class _Occurrence:
    source: str
    tier: str
    recognition: float
    snippet: str
    origin: str
    sense: SenseChoice
    line_no: int | None = None
    # When there is no line to place it (a job title not found verbatim).
    section: str | None = None


@dataclass
class EvidenceRecord:
    canonical_skill_id: UUID
    sense: str | None
    sense_resolved: bool
    sources: list[str]
    best_source: str
    match_tier: str
    recognition: float
    evidence: list[dict] = field(default_factory=list)


def classify_llm_match(raw_text: str, match_tier: str) -> tuple[str, str, str]:
    """(source, snippet text, origin) for a v1 match, from the origin its raw text records."""
    text = raw_text or ""
    if _INFERRED_MARKER in text and text.endswith(")"):
        quote = text.split(_INFERRED_MARKER, 1)[1][:-1]
        return SOURCE_INFERRED, quote, "inferred"
    if _FROM_MARKER in text and text.endswith('")'):
        phrase = text.split(_FROM_MARKER, 1)[1][:-2]
        return SOURCE_DERIVED, phrase, "llm-phrase-split"
    return SOURCE_LLM, text, "llm-skills"


def _llm_recognition(source: str, tier: str, recognition: dict) -> float:
    if source == SOURCE_LLM:
        return recognition[SOURCE_LLM] if tier in _STRONG_TIERS else recognition["LLM_WEAK"]
    return recognition[source]


def _find_line(lines: list[str], needle: str) -> int | None:
    """First line holding the needle - also across a line wrap (needle split over line i and i+1)."""
    key = squash_words(needle)
    if len(key.strip()) < 2:
        return None
    found = next((i for i, line in enumerate(lines) if key in squash_words(line)), None)
    if found is not None:
        return found
    prefix = squash_words(" ".join(needle.split()[:8]))
    return next((i for i in range(len(lines) - 1)
                 if prefix in squash_words(f"{lines[i]} {lines[i + 1]}")), None)


_WRAP_ENDINGS = (",", "&", "/", "-", " and", " or", " of", " the", " for", " to", " with", " in", " across", " including")
_MAX_WRAP_JOINS = 2


def _is_wrapped(line: str, next_line: str | None) -> bool:
    if next_line is None or not next_line.strip() or heading_section(next_line):
        return False
    text = line.rstrip()
    first = next_line.lstrip()[:1]
    return (text.count("(") > text.count(")") or text.lower().endswith(_WRAP_ENDINGS)
            or first.islower() or first == ")")


def excerpt_at(lines: list[str], line_no: int) -> str:
    """The line, joined with the following line(s) when it visibly continues there ("(In" + "Progress)")."""
    parts = [lines[line_no].strip()]
    index = line_no
    while len(parts) <= _MAX_WRAP_JOINS and _is_wrapped(lines[index], lines[index + 1] if index + 1 < len(lines) else None):
        index += 1
        parts.append(lines[index].strip())
    return " ".join(parts)


def build_evidence(
    *,
    text: str,
    index: ScanIndex,
    resolver: SkillSenseResolver,
    llm_matches: list[LLMSkillMatch],
    recognition: dict | None = None,
    blocked: frozenset = frozenset(),
    patterns: list | None = None,
    job_titles: list[str] | None = None,
) -> list[EvidenceRecord]:
    """
    blocked: (term_key, canonical_skill_id) pairs never accepted from any
    source - known wrong matches such as "IT governance" -> AI Governance
    (platform_config SCORING_V2_MATCH_BLOCKLIST).
    """
    recognition = {**DEFAULT_RECOGNITION, **(recognition or {})}
    lines = (text or "").splitlines()
    occurrences: dict[UUID, list[_Occurrence]] = {}

    line_sections = [section_of_line(lines, i) for i in range(len(lines))]
    for hit in scan_text(text, index) + pattern_hits(lines, line_sections, patterns or []):
        if (term_key(hit.term), hit.skill_id) in blocked:
            continue
        source = SOURCE_DERIVED if hit.origin == ORIGIN_DERIVED else SOURCE_SCAN
        sense = resolver.resolve_occurrence(hit.skill_id, lines, hit.line_no, hit.term)
        if hit.from_phrase:
            # Siblings in the distributed phrase are the strongest cue
            # ("Change, Incident & Problem Management" -> ITSM).
            phrase_sense = resolver.resolve_snippet(hit.skill_id, hit.from_phrase, hit.term)
            if phrase_sense.resolved and phrase_sense.sense:
                sense = phrase_sense
        occurrences.setdefault(hit.skill_id, []).append(_Occurrence(
            source, "SCAN" if source == SOURCE_SCAN else "DERIVED", recognition[source],
            excerpt_at(lines, hit.line_no),
            "text-scan" if source == SOURCE_SCAN else (hit.note or f'distributed from "{hit.from_phrase}"'), sense,
            hit.line_no,
        ))

    for hit in title_pattern_hits(job_titles or [], patterns or []):
        if (term_key(hit.title), hit.skill_id) in blocked:
            continue
        line_no = _find_line(lines, hit.title)
        snippet = excerpt_at(lines, line_no) if line_no is not None else hit.title
        sense = (resolver.resolve_occurrence(hit.skill_id, lines, line_no, hit.title) if line_no is not None
                 else resolver.resolve_snippet(hit.skill_id, hit.title, hit.title))
        occurrences.setdefault(hit.skill_id, []).append(_Occurrence(
            SOURCE_DERIVED, "DERIVED", recognition[SOURCE_DERIVED], snippet,
            f"job title pattern rule '{hit.rule_name}'", sense, line_no,
            section=None if line_no is not None else SECTION_EXPERIENCE,
        ))

    for match in llm_matches:
        if match.canonical_skill_id is None:
            continue
        own = match.raw_text.split(" (", 1)[0]
        if (term_key(own), match.canonical_skill_id) in blocked:
            continue
        source, snippet, origin = classify_llm_match(match.raw_text, match.match_tier)
        line_no = _find_line(lines, snippet)
        if line_no is not None:
            sense = resolver.resolve_occurrence(match.canonical_skill_id, lines, line_no, own)
            snippet = excerpt_at(lines, line_no)
        else:
            sense = resolver.resolve_snippet(match.canonical_skill_id, snippet, own)
        occurrences.setdefault(match.canonical_skill_id, []).append(_Occurrence(
            source, match.match_tier, _llm_recognition(source, match.match_tier, recognition), snippet, origin, sense,
            line_no,
        ))

    records: list[EvidenceRecord] = []
    for skill_id, items in occurrences.items():
        groups: dict[str | None, list[_Occurrence]] = {}
        for item in items:
            groups.setdefault(item.sense.sense if item.sense.resolved else None, []).append(item)
        senses_known = resolver.has_senses(skill_id)
        for sense, group in groups.items():
            best = min(group, key=lambda o: (-o.recognition, _SOURCE_ORDER[o.source]))
            snippets, seen = [], set()
            # An exact scan hit in a strong section first: it is what the
            # core confirmation auto-confirms on, so it must survive the cap.
            for item in sorted(group, key=lambda o: (not _strong_scan(o, lines), -o.recognition, _SOURCE_ORDER[o.source])):
                key = term_key(item.snippet)
                if key in seen:
                    continue
                seen.add(key)
                snippets.append({
                    "text": item.snippet[:_SNIPPET_CHARS], "source": item.source, "origin": item.origin,
                    "section": section_of_line(lines, item.line_no) if item.line_no is not None else item.section,
                    "heading": heading_text_of_line(lines, item.line_no) if item.line_no is not None else None,
                })
            records.append(EvidenceRecord(
                canonical_skill_id=skill_id,
                sense=sense,
                sense_resolved=(not senses_known) or sense is not None,
                sources=sorted({o.source for o in group}, key=_SOURCE_ORDER.get),
                best_source=best.source,
                match_tier=best.tier,
                recognition=best.recognition,
                evidence=snippets[:_MAX_SNIPPETS],
            ))
    return records


def to_rows(resume_id: UUID, records: list[EvidenceRecord], *, text: str, parse: ParseInfo) -> list[ResumeSkillEvidence]:
    digest = text_hash(text)
    return [
        ResumeSkillEvidence(
            resume_id=resume_id,
            canonical_skill_id=r.canonical_skill_id,
            sense=r.sense,
            sense_resolved=r.sense_resolved,
            sources=r.sources,
            best_source=r.best_source,
            match_tier=r.match_tier,
            recognition=r.recognition,
            evidence=r.evidence,
            text_hash=digest,
            detection_version=DETECTION_VERSION,
            parse_provider=parse.provider,
            parse_model=parse.model,
            parse_used_fallback=parse.used_fallback,
        )
        for r in records
    ]


@dataclass(frozen=True)
class DetectionTools:
    index: ScanIndex
    resolver: SkillSenseResolver
    blocked: frozenset = frozenset()
    patterns: tuple = ()


def resolve_blocklist(entries: list[dict], catalog: list) -> frozenset:
    """[{"term", "skill"}] (skill by canonical name) -> {(term_key, skill_id)}; unknown skill names are skipped."""
    ids = {skill.canonical_name.lower(): skill.id for skill in catalog}
    return frozenset(
        (term_key(entry["term"]), ids[str(entry["skill"]).lower()])
        for entry in entries or []
        if isinstance(entry, dict) and entry.get("term") and str(entry.get("skill", "")).lower() in ids
    )


class SkillEvidenceService:
    """Loads the catalogue/rules/senses and persists build_evidence() for one resume."""

    def __init__(self, detection_repository, skill_repository, cache_service=None):
        self.detection_repository = detection_repository
        self.skill_repository = skill_repository
        self.cache_service = cache_service

    def load_tools(self) -> "DetectionTools":
        from app.services.skills.skill_normalization_service import load_cached_active_skills
        catalog = load_cached_active_skills(self.skill_repository, self.cache_service)
        return DetectionTools(
            index=ScanIndex.build(catalog, self.detection_repository.load_scan_rules()),
            resolver=SkillSenseResolver(self.detection_repository.load_senses()),
            blocked=resolve_blocklist(self.detection_repository.load_match_blocklist(), catalog),
            patterns=tuple(self.detection_repository.load_pattern_rules()),
        )

    def detect(
        self, resume_id: UUID, *, text: str, llm_matches: list[LLMSkillMatch], parse: ParseInfo,
        persist: bool = True, job_titles: list[str] | None = None,
    ) -> list[EvidenceRecord]:
        tools = self.load_tools()
        records = build_evidence(text=text, index=tools.index, resolver=tools.resolver,
                                 llm_matches=llm_matches, blocked=tools.blocked, patterns=list(tools.patterns),
                                 job_titles=job_titles)
        if persist:
            self.detection_repository.replace_evidence(resume_id, to_rows(resume_id, records, text=text, parse=parse))
        return records


def build_v2_detection(db, skill_repository, cache_service=None):
    """(SkillEvidenceService, SkillDetectionRepository) for ResumeProcessingPipeline's v2 hooks."""
    from app.repositories.skill_detection_repository import SkillDetectionRepository
    detection_repository = SkillDetectionRepository(db)
    return SkillEvidenceService(detection_repository, skill_repository, cache_service), detection_repository


def redacted_resume_text(resume, storage_service=None) -> str:
    """
    The same text the pipeline gives the LLM: the stored file's extracted
    text, normalised, with PII redacted - all local, no LLM call.
    """
    from app.core.storage_service import StorageService
    from app.services.ai.preprocessing_service import PreprocessingService
    from app.services.pii.pii_detection_service import PIIDetectionService
    from app.services.pii.pii_redaction_service import PIIRedactionService
    from app.services.resume.resume_service import ResumeService
    from app.services.resume.resume_text_extraction_service import ResumeTextExtractionService
    from app.tasks.resume_processing_tasks import _FILE_FORMAT_TO_SOURCE_FORMAT

    content = (storage_service or StorageService()).download_file(
        bucket_name=ResumeService.RESUME_STORAGE_BUCKET, file_path=resume.file_path)
    text = PreprocessingService().normalize(
        ResumeTextExtractionService.extract(content, _FILE_FORMAT_TO_SOURCE_FORMAT[resume.file_format]))
    return PIIRedactionService().redact(text, PIIDetectionService().detect(text))


def job_titles_of(parsed) -> list[str]:
    """Work-experience titles from a parse (parsed_json dict or ResumeExtractionResponse)."""
    if isinstance(parsed, dict):
        entries = parsed.get("work_experience")
    else:
        entries = getattr(parsed, "work_experience", None)
    titles = []
    for entry in entries or []:
        title = entry.get("title") if isinstance(entry, dict) else getattr(entry, "title", None)
        if title:
            titles.append(str(title))
    return titles


def stored_llm_matches(skill_repository, resume_id: UUID) -> list[LLMSkillMatch]:
    """The stored v1 parse's matched skills (candidate_skills), as LLM-side input to build_evidence."""
    return [
        LLMSkillMatch(s.canonical_skill_id, s.raw_extracted_text, s.match_tier)
        for s in skill_repository.get_candidate_normalized_skills(resume_id)
        if s.canonical_skill_id is not None
    ]


def detect_stored_resume(service: SkillEvidenceService, resume, *, persist: bool = True) -> tuple[list, str]:
    """
    v2 evidence for a resume parsed before v2 detection existed: text from the
    stored file + the stored parse. Returns (records, redacted text).
    """
    text = redacted_resume_text(resume)
    records = service.detect(
        resume.id, text=text, llm_matches=stored_llm_matches(service.skill_repository, resume.id),
        job_titles=job_titles_of(resume.parsed_json),
        # The stored parse predates per-parse model tracking - said so, not left blank.
        parse=ParseInfo(provider="STORED_PARSE", model="unknown (parsed before v2 detection)"), persist=persist,
    )
    return records, text
