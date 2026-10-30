"""
The detection version stored with scoring v2 evidence: the code's DETECTION_VERSION plus a
fingerprint of the rule data detection reads - the catalogue's names and aliases, scan terms
and their cue words, sense cue words, the match blocklist, pattern rules, certification
mappings and the evidence verb lists. Any change to that data gives a new version, so stored
evidence becomes stale and the next scoring (a score-only re-score included) detects it again.

Only what changes detection counts: a skill's description or a sense's label does not.
The confirmation cache and the parse cache keep the plain DETECTION_VERSION - votes are
keyed per skill by the evidence they saw, and parsing doesn't read these rules.
"""
import dataclasses
import hashlib
import json
from uuid import UUID

from app.services.skills.evidence_rules import NOT_VERBS, OTHER_VERBS
from app.services.skills.resume_skill_scanner import DETECTION_VERSION

FINGERPRINT_CHARS = 8          # "2026.10.4+1a2b3c4d" fits detection_version String(20)


def _canon(value):
    """A JSON-ready form that doesn't depend on set or dict order."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _canon(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(k): _canon(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (set, frozenset)):
        return sorted((_canon(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True, default=str))
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    if isinstance(value, UUID):
        return str(value)
    return value


def _sorted(items) -> list:
    return sorted((_canon(i) for i in items), key=lambda v: json.dumps(v, sort_keys=True, default=str))


def _attr(item, name):
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def detection_fingerprint(*, catalog, scan_rules: dict, senses, blocked, patterns, certifications, rules) -> str:
    payload = {
        "catalog": _sorted([_attr(s, "id"), _attr(s, "canonical_name"), sorted(_attr(s, "aliases") or [])]
                           for s in catalog),
        "scan_terms": _sorted([term, rule.policy, sorted(rule.cue_words or ())] for term, rule in scan_rules.items()),
        # cue words, the default and the window decide a sense; its label and description don't
        "senses": _sorted([d.skill_id, d.sense_key, sorted(d.cue_words or ()), d.is_default, d.window_lines]
                          for d in senses),
        "blocklist": _sorted(blocked),
        "patterns": _sorted(patterns),
        "certifications": _sorted(certifications),
        "evidence": {"qualifiers": sorted(rules.qualifiers), "work_verbs": sorted(rules.work_verbs),
                     "non_work_verbs": sorted(rules.non_work_verbs), "other_verbs": sorted(OTHER_VERBS),
                     "not_verbs": sorted(NOT_VERBS)},
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return digest[:FINGERPRINT_CHARS]


def detection_version(fingerprint: str) -> str:
    return f"{DETECTION_VERSION}+{fingerprint}"
