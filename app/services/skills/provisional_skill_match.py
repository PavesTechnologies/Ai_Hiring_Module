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


def parsed_json_text(parsed_json: dict | None) -> str:
    """
    Text for v1 provisional matching, from the stored parse. Each listed
    skill / certification / project technology goes on its own line, so it
    is a whole list item to the scanner; free text keeps its own lines.
    """
    parsed = parsed_json if isinstance(parsed_json, dict) else {}
    lines: list[str] = []

    def add(value):
        if isinstance(value, str) and value.strip():
            lines.extend(part for part in value.splitlines() if part.strip())

    for key in ("skills", "soft_skills", "certifications"):
        for item in parsed.get(key) or []:
            add(item)
    for item in parsed.get("inferred_skills") or []:
        if isinstance(item, dict):
            add(item.get("skill"))
            add(item.get("evidence"))
    add(parsed.get("summary"))
    for entry in parsed.get("work_experience") or []:
        if isinstance(entry, dict):
            add(entry.get("title"))
            add(entry.get("description"))
    for project in parsed.get("projects") or []:
        if isinstance(project, dict):
            add(project.get("name"))
            add(project.get("description"))
            for tech in project.get("tech") or []:
                add(tech)
    return "\n".join(lines)
