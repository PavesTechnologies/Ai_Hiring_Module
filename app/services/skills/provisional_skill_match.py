"""
Provisional scoring of a JD's unresolved unknown skills (Phase 2 Step 2).

Until HR maps, approves or drops an unknown skill, it still counts in the
skill score - never in a gate - at its tier weight x PROVISIONAL_CREDIT when
the resume shows it, 0 when it doesn't. Matching reuses the v2 catalogue
scanner unchanged (resume_skill_scanner): the unknown skills are indexed as
if they were catalogue skills, so the same rules apply - terms of 2 or fewer
letters are not scanned, skill_scan_terms REQUIRE_CUE terms need a cue word
nearby, EXCLUDE terms never match, plain words only as whole list items,
British/American spelling normalised.

  v2: the redacted resume text, during detection (stored per resume in
      resume_provisional_skill_scans - the text itself is never stored).
  v1: text rebuilt from the stored parse (Resume.parsed_json).
"""
from dataclasses import dataclass
from types import SimpleNamespace
from uuid import UUID

from app.services.skills.resume_skill_scanner import ScanIndex, ScanTermRule, scan_text

PROVISIONAL_CREDIT = 0.8
_EXCERPT_CHARS = 240


@dataclass(frozen=True)
class ProvisionalTerm:
    unknown_skill_id: UUID
    raw_text: str


def build_provisional_index(terms: list[ProvisionalTerm], rules: dict[str, ScanTermRule] | None) -> ScanIndex:
    catalog = [SimpleNamespace(id=t.unknown_skill_id, canonical_name=t.raw_text, aliases=[]) for t in terms]
    return ScanIndex.build(catalog, rules or {})


def scan_provisional(text: str, terms: list[ProvisionalTerm],
                     rules: dict[str, ScanTermRule] | None) -> dict[UUID, dict]:
    """unknown_skill_id -> first hit {"unknown_skill_id", "term", "origin", "excerpt"} for each term found."""
    if not terms or not text:
        return {}
    found: dict[UUID, dict] = {}
    for hit in scan_text(text, build_provisional_index(terms, rules)):
        if hit.skill_id not in found:
            found[hit.skill_id] = {"unknown_skill_id": str(hit.skill_id), "term": hit.term,
                                   "origin": hit.origin, "excerpt": hit.line.strip()[:_EXCERPT_CHARS]}
    return found


