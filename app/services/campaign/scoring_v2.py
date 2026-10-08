"""
Scoring v2 (Phase 1) - deterministic score and decision from v2 evidence.

Pure: takes the JD's skill rows, the skill hierarchy, the resume's v2
evidence (resume_skill_evidence) and the core-skill confirmation results,
plus the experience/education/functional results v1 already computes, and
returns a full breakdown. Nothing here reads or writes the DB.

What changes from v1:
  A  core skills are SCORED as well as gated, at importance weight x2
     (supporting x1); preferred skills keep their 10% share.
  B  experience and education are hard gates only - out of the average.
     The average is skills 70 / functional 15, renormalised.
  C  hierarchy multipliers, importance weights, recognition, preferred
     share and component weights come from platform_config
     (SCORING_V2_CONFIG), today's values as defaults; every result stores
     the exact config used and its version hash.

Core gate - only CONFIRMED core matches count. Per core skill:
  CONFIRMED       counts for the gate; full score credit (x recognition)
  UNCERTAIN       the confirmation votes split: no gate credit; supporting
                  credit at recognition 0.8; the candidate is flagged
                  would_be_review when it fails
  UNCONFIRMED /   no gate credit; score credit as supporting evidence
  NOT_RUN           ("found, not confirmed as core")
  REJECTED        the LLM said no: the evidence is discarded entirely - no
                  gate credit, no score credit, and it can't serve as a
                  child/sibling match for another skill
  SENSE_MISMATCH  no gate credit; at most sibling credit in the score
  NOT_FOUND       hierarchy tiers only (child/grandchild/sibling); a core
                  skill matched only through the hierarchy gets score
                  credit but no gate credit (never confirmed)

REVIEW: exactly one near-miss (near_miss.py - one core skill over the limit,
counting uncertain core matches, or experience short within
max(1 year, 10%)) gives outcome REVIEW; the candidate goes on to semantic/AI
and is capped at HOLD there. would_be_review mirrors it.
"""
import copy
import hashlib
import json
from dataclasses import dataclass, field
from uuid import UUID

from app.services.campaign.near_miss import OUTCOME_PASS as NEAR_MISS_PASS, NearMissDecision, evaluate_near_miss

SCORING_VERSION = "v2"

# Go-live prerequisite (2026-10-01): v2 may only DECIDE once the REVIEW
# outcome exists, so borderline candidates land on PASS or REVIEW, never
# REJECT. REVIEW shipped (near_miss.py): a scoring_v2_enabled campaign now
# lets v2 decide. Shadow-only campaigns are unaffected.
REVIEW_OUTCOME_AVAILABLE = True
SCORING_V2_CONFIG_KEY = "SCORING_V2_CONFIG"

DEFAULT_CONFIG: dict = {
    # A: core skills weigh double in the skill score.
    "importance_weights": {"core": 2.0, "supporting": 1.0, "unclassified": 1.0},
    # C: today's hierarchy multipliers (v1 hardcodes all but grandchild).
    "hierarchy_multipliers": {"EXACT": 1.0, "CHILD": 0.7, "GRANDCHILD": 0.5, "SIBLING": 0.4, "SEMANTIC": 0.2},
    # Evidence recognition (see skill_evidence_service).
    "recognition": {"SCAN": 1.0, "LLM": 1.0, "LLM_WEAK": 0.8, "DERIVED": 0.8, "INFERRED": 0.8,
                    # A core match the confirmation votes split on.
                    "UNCERTAIN": 0.8},
    "preferred_share": 0.10,
    # B: experience/education are gates only.
    "component_weights": {"skills": 0.70, "functional": 0.15},
    # Old informational "would be REVIEW" margins - no longer read; the REVIEW
    # rule lives in near_miss.py. Kept so existing config versions still hash.
    "near_miss": {"extra_missing_core": 1, "experience_years": 1.0, "score_points": 5.0},
}

STATUS_CONFIRMED = "CONFIRMED"
STATUS_REJECTED = "REJECTED"
STATUS_UNCONFIRMED = "UNCONFIRMED"
STATUS_UNCERTAIN = "UNCERTAIN"
STATUS_NOT_RUN = "NOT_RUN"
STATUS_NOT_FOUND = "NOT_FOUND"
STATUS_SENSE_MISMATCH = "SENSE_MISMATCH"

OUTCOME_PASS = "PASS"
OUTCOME_REJECT = "REJECT"
# Exactly one near-miss (near_miss.py): let through to semantic/AI, capped at HOLD.
OUTCOME_REVIEW = "REVIEW"
# The core confirmation couldn't run (quota, provider down, backup failing
# too): no decision - the candidate is retried. Never a REJECT.
OUTCOME_PENDING = "PENDING"
# v2 skipped entirely (daily LLM call cap reached) - shown "not run".
OUTCOME_NOT_RUN = "NOT_RUN"

_DEFINITE_JD_SENSE = {"cue", "title"}


def apply_unavailable_outcome(breakdown: dict, outcome: str, reason: str) -> dict:
    """
    PENDING (confirmation couldn't run) / NOT_RUN (daily cap) override the
    decision: no PASS/REJECT, no review flag. The computed score is kept as
    provisional_score for diagnosis only.
    """
    breakdown.update(outcome=outcome, outcome_reason=reason, deterministic_passed=None, would_be_review=False,
                     provisional_score=breakdown.get("deterministic_score"))
    if outcome == OUTCOME_NOT_RUN:
        breakdown["deterministic_score"] = None
    return breakdown


def merge_config(configured: dict | None) -> dict:
    """DEFAULT_CONFIG overlaid with the configured values (one level of nested dicts)."""
    merged = copy.deepcopy(DEFAULT_CONFIG)
    for key, value in (configured or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def parse_config(raw: str | None) -> dict:
    if not raw:
        return merge_config(None)
    try:
        value = json.loads(raw)
    except ValueError:
        return merge_config(None)
    return merge_config(value if isinstance(value, dict) else None)


def config_version(snapshot: dict) -> str:
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class JDSkillInput:
    canonical_skill_id: UUID
    name: str
    mandatory: bool
    weight: float | None
    importance: str | None                 # "core" / "supporting" / None
    alias_skill_ids: tuple[UUID, ...] = ()
    sense: str | None = None
    sense_basis: str | None = None

    @property
    def own_ids(self) -> tuple[UUID, ...]:
        return (self.canonical_skill_id, *self.alias_skill_ids)


@dataclass(frozen=True)
class EvidenceInput:
    canonical_skill_id: UUID
    sense: str | None
    sources: tuple[str, ...]
    best_source: str
    match_tier: str
    recognition: float
    evidence_text: str = ""


@dataclass
class ScoringInputs:
    jd_skills: list[JDSkillInput]
    evidence: list[EvidenceInput]
    # canonical_skill_id (str) -> confirmation result dict (status, quote, ...)
    confirmations: dict[str, dict]
    # skill id -> object with .id / .canonical_name / .parent_skill_id / .is_active
    # and children_by_parent / grandchildren_by_parent as in
    # CandidateScoringService.load_mandatory_skill_hierarchy
    hierarchy: dict
    experience_result: dict | None = None
    education_result: dict | None = None
    domain_result: dict | None = None
    deterministic_threshold: float = 0.0
    required_skill_coverage_threshold: float = 0.0
    allowed_missing_core: int = 0
    score_threshold_rejects: bool = False
    extra: dict = field(default_factory=dict)


def definite_mismatch(skill: JDSkillInput, resume_sense: str | None) -> bool:
    return bool(skill.sense and resume_sense and resume_sense != skill.sense and skill.sense_basis in _DEFINITE_JD_SENSE)


def _recognition(ev: EvidenceInput, config: dict) -> float:
    rec = config["recognition"]
    if ev.best_source == "LLM":
        return float(rec["LLM"] if ev.match_tier in {"EXACT", "ALIAS", "CASE_INSENSITIVE", "RULE_BASED", "MANUAL_HR"}
                     else rec["LLM_WEAK"])
    return float(rec.get(ev.best_source, ev.recognition))


def _best(evs: list[EvidenceInput], config: dict) -> EvidenceInput | None:
    return max(evs, key=lambda e: _recognition(e, config)) if evs else None


def _importance_weight(label: str | None, config: dict) -> float:
    return float(config["importance_weights"].get(label or "unclassified", config["importance_weights"]["unclassified"]))


def _entry(skill: JDSkillInput, config: dict, match_type: str, multiplier: float, ev: EvidenceInput | None,
           *, status: str | None = None, gate: bool = False, matched_name: str | None = None) -> dict:
    weight = float(skill.weight) if skill.weight is not None else 1.0
    effective = round(weight * _importance_weight(skill.importance, config), 4)
    recognition = _recognition(ev, config) if ev is not None else 0.0
    if status == STATUS_UNCERTAIN and match_type == "EXACT":
        recognition = float(config["recognition"]["UNCERTAIN"])
    quality = round(multiplier * recognition, 4) if match_type != "MISSING" else 0.0
    return {
        "canonical_skill_id": str(skill.canonical_skill_id),
        "canonical_name": skill.name,
        "mandatory": skill.mandatory,
        "importance": skill.importance,
        "configured_weight": skill.weight,
        "effective_weight": effective,
        "match_type": match_type,
        "hierarchy_multiplier": multiplier,
        "recognition": recognition,
        "quality": quality,
        "contribution": round(effective * quality, 4),
        "matched_skill_name": matched_name,
        "evidence_sources": list(ev.sources) if ev is not None else [],
        "evidence_text": ev.evidence_text if ev is not None else None,
        "jd_sense": skill.sense,
        "jd_sense_basis": skill.sense_basis,
        "confirmation_status": status,
        "counts_for_core_gate": gate,
    }


def _hierarchy_match(skill: JDSkillInput, pool: dict, hierarchy: dict, config: dict):
    """(match_type, EvidenceInput, matched skill name) for the best CHILD -> GRANDCHILD -> SIBLING match, else None."""
    mult = config["hierarchy_multipliers"]
    children = [c for c in hierarchy.get("children_by_parent", {}).get(skill.canonical_skill_id, []) if c.is_active]
    for match_type, candidates in (
        ("CHILD", children),
        ("GRANDCHILD", [g for c in children for g in hierarchy.get("grandchildren_by_parent", {}).get(c.id, [])
                        if g.is_active]),
    ):
        found = [(c, _best(pool.get(c.id, []), config)) for c in candidates if pool.get(c.id)]
        if found:
            node, ev = max(found, key=lambda pair: _recognition(pair[1], config))
            return match_type, float(mult[match_type]), ev, node.canonical_name
    target = hierarchy.get("skills_by_id", {}).get(skill.canonical_skill_id)
    if target is not None and getattr(target, "parent_skill_id", None) is not None:
        siblings = [s for s in hierarchy.get("children_by_parent", {}).get(target.parent_skill_id, [])
                    if s.id != skill.canonical_skill_id and s.is_active and pool.get(s.id)]
        if siblings:
            node, ev = max(((s, _best(pool[s.id], config)) for s in siblings),
                           key=lambda pair: _recognition(pair[1], config))
            return "SIBLING", float(mult["SIBLING"]), ev, node.canonical_name
    return None


def _ratio(entries: list[dict]) -> float | None:
    total = sum(e["effective_weight"] for e in entries)
    return (sum(e["contribution"] for e in entries) / total * 100) if total > 0 else None


def score_v2(inputs: ScoringInputs, config: dict) -> dict:
    mult = config["hierarchy_multipliers"]
    mandatory = [s for s in inputs.jd_skills if s.mandatory]
    preferred = [s for s in inputs.jd_skills if not s.mandatory]
    core = [s for s in mandatory if s.importance == "core"]

    # Evidence pool by skill id. A REJECTED core match is discarded
    # entirely, before anything else can use it.
    discarded: set[UUID] = set()
    for skill in core:
        if (inputs.confirmations.get(str(skill.canonical_skill_id)) or {}).get("status") == STATUS_REJECTED:
            discarded.update(skill.own_ids)
    pool: dict[UUID, list[EvidenceInput]] = {}
    for ev in inputs.evidence:
        if ev.canonical_skill_id not in discarded:
            pool.setdefault(ev.canonical_skill_id, []).append(ev)

    entries = []
    for skill in mandatory:
        own = [ev for skill_id in skill.own_ids for ev in pool.get(skill_id, [])]
        compatible = [ev for ev in own if not definite_mismatch(skill, ev.sense)]
        mismatched = [ev for ev in own if definite_mismatch(skill, ev.sense)]
        is_core = skill.importance == "core"
        status = None
        if is_core:
            status = (inputs.confirmations.get(str(skill.canonical_skill_id)) or {}).get("status")
            if skill.canonical_skill_id in discarded:
                status = STATUS_REJECTED
            elif compatible and status not in (STATUS_CONFIRMED, STATUS_UNCONFIRMED, STATUS_UNCERTAIN):
                status = STATUS_NOT_RUN
            elif not compatible and mismatched:
                status = STATUS_SENSE_MISMATCH
            elif not own:
                status = STATUS_NOT_FOUND

        if compatible:
            confirmed = is_core and status == STATUS_CONFIRMED
            entries.append(_entry(skill, config, "EXACT", float(mult["EXACT"]), _best(compatible, config),
                                  status=status, gate=confirmed if is_core else True, matched_name=skill.name))
            continue

        options = []
        if mismatched:
            options.append(("SENSE_MISMATCH", float(mult["SIBLING"]), _best(mismatched, config), skill.name))
        hierarchy_hit = _hierarchy_match(skill, pool, inputs.hierarchy, config)
        if hierarchy_hit:
            options.append(hierarchy_hit)
        if options:
            match_type, multiplier, ev, name = max(options, key=lambda o: o[1] * _recognition(o[2], config))
            # Hierarchy/sense credit never satisfies a core skill's gate; for
            # a non-core skill a covered match counts as covered (as in v1).
            entries.append(_entry(skill, config, match_type, multiplier, ev, status=status,
                                  gate=not is_core and match_type != "SENSE_MISMATCH", matched_name=name))
        else:
            entries.append(_entry(skill, config, "MISSING", 0.0, None, status=status, gate=False))

    preferred_entries = []
    for skill in preferred:
        own = [ev for skill_id in skill.own_ids for ev in pool.get(skill_id, [])
               if not definite_mismatch(skill, ev.sense)]
        preferred_entries.append(
            _entry(skill, config, "EXACT", float(mult["EXACT"]), _best(own, config), matched_name=skill.name)
            if own else _entry(skill, config, "MISSING", 0.0, None))

    required_score = _ratio(entries)
    preferred_score = _ratio(preferred_entries)
    share = float(config["preferred_share"])
    if required_score is None and preferred_score is None:
        skill_score = None
    elif preferred_score is None:
        skill_score = required_score
    elif required_score is None:
        skill_score = preferred_score
    else:
        skill_score = (1 - share) * required_score + share * preferred_score

    weights = config["component_weights"]
    components = [] if skill_score is None else [("skills", skill_score, float(weights["skills"]))]
    domain = inputs.domain_result or {}
    if domain.get("applicable"):
        components.append(("functional", float(domain["score"]), float(weights["functional"])))
    weight_sum = sum(w for _, _, w in components)
    final_score = round(sum(v * w for _, v, w in components) / weight_sum, 2) if weight_sum > 0 else 100.0

    # Gates.
    if core:
        core_confirmed = sum(1 for e in entries if e["importance"] == "core" and e["counts_for_core_gate"])
        missing_core = len(core) - core_confirmed
        core_gap_passed = missing_core <= inputs.allowed_missing_core
        coverage_pct = round(core_confirmed / len(core) * 100, 2)
    else:
        covered = sum(1 for e in entries if e["counts_for_core_gate"])
        core_confirmed, missing_core, core_gap_passed = 0, 0, True
        coverage_pct = round(covered / len(entries) * 100, 2) if entries else 100.0
    coverage_passed = coverage_pct >= float(inputs.required_skill_coverage_threshold)
    experience = inputs.experience_result or {}
    education = inputs.education_result or {}
    experience_passed = experience.get("passed", True) is not False
    education_passed = education.get("passed", True) is not False
    score_rejects = bool(inputs.score_threshold_rejects or not core)
    score_passed = (not score_rejects) or final_score >= float(inputs.deterministic_threshold)

    checks = [
        {"check": "Core skills", "passed": core_gap_passed, "applies": bool(core),
         "detail": f"{core_confirmed} of {len(core)} core skills confirmed; up to "
                   f"{inputs.allowed_missing_core} may be missing - {missing_core} missing."},
        {"check": "Core skill coverage" if core else "Required skill coverage", "passed": coverage_passed,
         "applies": float(inputs.required_skill_coverage_threshold) > 0,
         "detail": f"{coverage_pct}% covered; at least {inputs.required_skill_coverage_threshold}% required."},
        {"check": "Experience", "passed": experience_passed, "applies": bool(experience.get("applicable")),
         "detail": f"{experience.get('candidate_years')} years; minimum {experience.get('min_years')} required."},
        {"check": "Education", "passed": education_passed, "applies": bool(education.get("applicable"))},
        {"check": "Score threshold", "passed": score_passed, "applies": score_rejects,
         "detail": f"Score {final_score} vs threshold {inputs.deterministic_threshold}."},
    ]
    passed = core_gap_passed and coverage_passed and experience_passed and education_passed and score_passed

    failed = [c["check"] for c in checks if not c["passed"]]
    uncertain = [e["canonical_name"] for e in entries
                 if e["importance"] == "core" and e["confirmation_status"] == STATUS_UNCERTAIN]
    # REVIEW outcome - the same near-miss rule v1 uses. An uncertain core
    # match counts as missing for the gate above; here it is a near-miss
    # only when it is what takes the candidate over the limit.
    if passed:
        near_miss = NearMissDecision(NEAR_MISS_PASS)
    else:
        other_failed = [c["check"] for c in checks
                        if not c["passed"] and c["check"] not in ("Core skills", "Experience")]
        missing_names = [e["canonical_name"] for e in entries
                         if e["importance"] == "core" and not e["counts_for_core_gate"]]
        near_miss = evaluate_near_miss(
            core_skill_count=len(core), confirmed_core_count=core_confirmed,
            allowed_missing_core=inputs.allowed_missing_core,
            definite_missing_core=missing_core - len(uncertain), uncertain_core=len(uncertain),
            over_limit_names=missing_names, experience=experience, other_failed_checks=other_failed,
        )
    would_be_review = near_miss.needs_review

    def names(predicate):
        return [e["canonical_name"] for e in entries if e["importance"] == "core" and predicate(e)]

    return {
        "scoring_version": SCORING_VERSION,
        "outcome": OUTCOME_PASS if passed else (OUTCOME_REVIEW if would_be_review else OUTCOME_REJECT),
        # A REVIEW candidate goes on to semantic/AI (capped at HOLD there).
        "deterministic_passed": passed or would_be_review,
        "deterministic_score": final_score,
        "skill_score": round(skill_score, 2) if skill_score is not None else None,
        "required_skill_score": round(required_score, 2) if required_score is not None else None,
        "preferred_skill_score": round(preferred_score, 2) if preferred_score is not None else None,
        "functional_score": float(domain["score"]) if domain.get("applicable") else None,
        "components": [{"name": n, "score": round(v, 2), "weight": w} for n, v, w in components],
        "mandatory_skills": entries,
        "preferred_skills": preferred_entries,
        "core_skill_count": len(core),
        "core_confirmed_count": core_confirmed,
        "missing_core_skill_count": missing_core,
        "allowed_missing_core_skills": inputs.allowed_missing_core,
        "core_coverage_pct": coverage_pct,
        "core_confirmed": names(lambda e: e["counts_for_core_gate"]),
        # Shown as "found, not confirmed as core" - supporting evidence only.
        "core_found_not_confirmed": names(lambda e: e["confirmation_status"] in (STATUS_UNCONFIRMED, STATUS_NOT_RUN)),
        "core_rejected_by_llm": names(lambda e: e["confirmation_status"] == STATUS_REJECTED),
        # Votes split - supporting at 0.8, never toward the gate; flags review.
        "core_uncertain": uncertain,
        "core_sense_mismatch": names(lambda e: e["confirmation_status"] == STATUS_SENSE_MISMATCH),
        "core_via_hierarchy_only": names(lambda e: e["match_type"] in ("CHILD", "GRANDCHILD", "SIBLING")),
        "checks": checks,
        "failed_checks": failed,
        "would_be_review": would_be_review,
        **near_miss.as_breakdown(),
        "score_threshold_rejects": score_rejects,
        "deterministic_threshold": float(inputs.deterministic_threshold),
        "experience_validation": inputs.experience_result,
        "education_validation": inputs.education_result,
        **inputs.extra,
    }
