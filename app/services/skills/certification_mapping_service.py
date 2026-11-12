"""
Certification -> skill mapping (Phase 3 Step 5), scoring v2 only.

A resume's certification (parsed_json["certifications"], plain strings) that
matches a mapped certification gives DERIVED evidence for each of its
skills - never auto-confirmed: it always goes to the 3-vote core
confirmation. A certification still being worked towards never counts.

Name matching is by normalised words: lower case, (R)/(TM) dropped,
punctuation to spaces, letters split from digits, a "v" before a version
number dropped ("ITIL(R) v4 Foundation" == "ITIL 4 Foundation"). A variant
matches when its words appear contiguously in the certification string, so
"ITIL Foundation" doesn't match "ITIL 4 Foundation", and a bare "ITIL"
matches neither.

HR_ADMIN edits certifications, their name variants and their skills
(audited).
"""
import re
from dataclasses import dataclass
from uuid import UUID

from app.enums.constants import ActionType, EntityType
from app.exception_handler.exceptions import ConflictError, NotFoundError, UnprocessableError

_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_IN_PROGRESS = re.compile(
    r"\b(?:in[\s-]*progress|pursuing|ongoing|underway|appearing|scheduled)\b"
    # "expected" only when it gives a date: "expected 2027", "expected Dec 2026", "(expected: 03/2027)".
    rf"|\bexpected\b[\s:,(\-–]*(?:{_MONTH}\s*|\d{{1,2}}\s*[/.\-]\s*)?\d{{4}}\b",
    re.IGNORECASE,
)


def certification_words(text: str | None) -> tuple[str, ...]:
    s = (text or "").lower().replace("®", " ").replace("™", " ").replace("©", " ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", s)
    words = s.split()
    return tuple(w for i, w in enumerate(words)
                 if not (w == "v" and i + 1 < len(words) and words[i + 1].isdigit()))


def is_in_progress(certification: str | None) -> bool:
    return bool(_IN_PROGRESS.search(certification or ""))


_SEGMENT_SEPARATORS = ";|•·,"


def _segments(text: str) -> list[tuple[int, int]]:
    """(start, end) of the list items on a line: split at ; | • · and commas outside brackets."""
    spans, start, depth = [], 0, 0
    for i, ch in enumerate(text):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        elif ch in _SEGMENT_SEPARATORS and depth == 0:
            spans.append((start, i))
            start = i + 1
    spans.append((start, len(text)))
    return spans


def in_progress_in_text(text: str | None, certification: str | None) -> bool:
    """
    The resume's own text says this certification is still in progress -
    the parse can drop "(appearing, Dec 2026)" from the name. Only the
    certification's own item on the line is read, so "ITIL 4 Foundation;
    ServiceNow CSA (appearing)" keeps ITIL.
    """
    words = certification_words(certification)
    if not text or not words:
        return False
    pattern = re.compile(r"[^a-z0-9]*(?:v(?=\d))?".join(re.escape(w) for w in words), re.IGNORECASE)
    found = pattern.search(text)
    if found is None:
        return False
    return any(_IN_PROGRESS.search(text[start:end]) for start, end in _segments(text)
               if start < found.end() and found.start() < end)


def _contains(words: tuple[str, ...], variant: tuple[str, ...]) -> bool:
    n = len(variant)
    return n > 0 and any(words[i:i + n] == variant for i in range(len(words) - n + 1))


@dataclass(frozen=True)
class CertificationRule:
    name: str
    variants: tuple[tuple[str, ...], ...]          # normalised words of each name variant
    skills: tuple[tuple[UUID, str | None], ...]   # (canonical_skill_id, sense_key)


def rule_from(name: str, variants: list[str] | None, skills) -> CertificationRule:
    words = {certification_words(v) for v in [name, *(variants or [])]}
    return CertificationRule(name, tuple(sorted(w for w in words if w)), tuple(skills))


@dataclass(frozen=True)
class CertificationMatch:
    certification: str          # as written on the resume
    rule: CertificationRule


def match_certifications(certifications: list[str] | None, rules) -> list[CertificationMatch]:
    """The mapped certifications a resume holds - in-progress ones excluded."""
    matches = []
    for certification in certifications or []:
        if not isinstance(certification, str) or not certification.strip() or is_in_progress(certification):
            continue
        words = certification_words(certification)
        for rule in rules:
            if any(_contains(words, variant) for variant in rule.variants):
                matches.append(CertificationMatch(certification, rule))
    return matches


class CertificationMappingService:
    """HR_ADMIN editing of certifications, their name variants and skills (audited)."""

    def __init__(self, repository, skill_repository, senses_loader, audit_service):
        self.repository = repository
        self.skill_repository = skill_repository
        self.senses_loader = senses_loader            # () -> list[SenseDef]
        self.audit_service = audit_service

    def list_all(self):
        return self.repository.list_all()

    def add_certification(self, *, name: str, variants: list[str], note: str | None, actor_id: str,
                          actor_role: str | None):
        from app.models.scoring_v2 import Certification
        name = (name or "").strip()
        if not certification_words(name):
            raise UnprocessableError("A certification needs a name.")
        variants = list(dict.fromkeys(v.strip() for v in variants or [] if v and v.strip() and certification_words(v)))
        try:
            if self.repository.find_by_name(name) is not None:
                raise ConflictError(f"'{name}' is already a mapped certification.")
            certification = self.repository.add(Certification(name=name, name_variants=variants,
                                                              note=(note or "").strip() or None, created_by=actor_id))
            self._audit(ActionType.CERTIFICATION_ADDED, certification.id, actor_id, actor_role,
                        {"name": name, "name_variants": variants})
            self.repository.commit()
        except Exception:
            self.repository.rollback()
            raise
        return certification

    def _mark(self, skill_ids) -> int:
        """Settings phase 3: stamp scored candidates of live campaigns whose JD uses these skills out of date."""
        from app.services.settings.reference_impact import mark_after_edit
        return mark_after_edit(self.repository.db, "skill", "A certification mapping", skill_ids=list(skill_ids))

    def remove_certification(self, certification_id: UUID, *, actor_id: str, actor_role: str | None) -> None:
        try:
            certification = self._certification(certification_id)
            details = {"name": certification.name, "name_variants": list(certification.name_variants or []),
                       "skills": [str(s.canonical_skill_id) for s in certification.skills]}
            details["candidates_marked_out_of_date"] = self._mark(details["skills"])   # before the mappings go
            self.repository.delete(certification)
            self._audit(ActionType.CERTIFICATION_REMOVED, certification_id, actor_id, actor_role, details)
            self.repository.commit()
        except Exception:
            self.repository.rollback()
            raise

    def add_skill(self, certification_id: UUID, *, canonical_skill_id: UUID, sense_key: str | None, actor_id: str,
                  actor_role: str | None):
        from app.models.scoring_v2 import CertificationSkill
        try:
            certification = self._certification(certification_id)
            skill = self.skill_repository.get_skill_by_id(canonical_skill_id)
            if skill is None or not getattr(skill, "is_active", True):
                raise NotFoundError("Catalogue skill not found.")
            senses = {s.sense_key for s in self.senses_loader() if s.skill_id == canonical_skill_id}
            if sense_key and sense_key not in senses:
                raise UnprocessableError("That meaning doesn't belong to this skill.")
            if any(s.canonical_skill_id == canonical_skill_id for s in certification.skills):
                raise ConflictError(f"'{skill.canonical_name}' is already mapped from '{certification.name}'.")
            row = self.repository.add_skill(CertificationSkill(
                certification_id=certification.id, canonical_skill_id=canonical_skill_id,
                sense_key=sense_key or None, created_by=actor_id))
            self._audit(ActionType.CERTIFICATION_SKILL_ADDED, certification.id, actor_id, actor_role,
                        {"certification": certification.name, "skill": skill.canonical_name,
                         "canonical_skill_id": str(canonical_skill_id), "sense_key": sense_key or None,
                         "candidates_marked_out_of_date": self._mark([canonical_skill_id])})
            self.repository.commit()
        except Exception:
            self.repository.rollback()
            raise
        return row

    def remove_skill(self, certification_id: UUID, canonical_skill_id: UUID, *, actor_id: str,
                     actor_role: str | None) -> None:
        try:
            certification = self._certification(certification_id)
            row = next((s for s in certification.skills if s.canonical_skill_id == canonical_skill_id), None)
            if row is None:
                raise NotFoundError("That skill isn't mapped from this certification.")
            skill = self.skill_repository.get_skill_by_id(canonical_skill_id)
            self.repository.delete_skill(row)
            self._audit(ActionType.CERTIFICATION_SKILL_REMOVED, certification.id, actor_id, actor_role,
                        {"certification": certification.name,
                         "skill": getattr(skill, "canonical_name", str(canonical_skill_id)),
                         "canonical_skill_id": str(canonical_skill_id), "sense_key": row.sense_key,
                         "candidates_marked_out_of_date": self._mark([canonical_skill_id])})
            self.repository.commit()
        except Exception:
            self.repository.rollback()
            raise

    def _certification(self, certification_id: UUID):
        certification = self.repository.get(certification_id)
        if certification is None:
            raise NotFoundError("Certification not found.")
        return certification

    def _audit(self, action_type, entity_id, actor_id, actor_role, details) -> None:
        self.audit_service.log(actor_id=actor_id, actor_role=actor_role, action_type=action_type,
                               entity_type=EntityType.CERTIFICATION, entity_id=entity_id, details=details)
