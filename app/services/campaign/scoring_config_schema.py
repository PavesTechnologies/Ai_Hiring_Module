"""
What a scoring version's configuration may hold (settings phase 3): every section of DEFAULT_CONFIG with its
values' types and ranges, checked before a version is saved. merge_config overlays one level, so a section
given partly keeps the defaults for the rest; a value of the wrong type, out of range or unknown is refused,
with every problem listed. "evidence" stays shared by all versions (refused, see scoring_versions).
"""
from app.services.jd.jd_profile_templates import EVIDENCE_SECTION_OPTIONS


def _num(lo, hi):
    return ("number", lo, hi)


SCHEMA = {
    "importance_weights": {"core": _num(0, 10), "supporting": _num(0, 10), "unclassified": _num(0, 10)},
    "hierarchy_multipliers": {k: _num(0, 1) for k in ("EXACT", "CHILD", "GRANDCHILD", "SIBLING", "SEMANTIC")},
    "recognition": {k: _num(0, 1) for k in ("SCAN", "LLM", "LLM_WEAK", "DERIVED", "INFERRED", "UNCERTAIN")},
    "preferred_share": _num(0, 1),
    "component_weights": {"skills": _num(0, 1), "functional": _num(0, 1), "education_preferred": _num(0, 1)},
    # No longer read (kept so older versions still hash); validated loosely.
    "near_miss": {"extra_missing_core": _num(0, 50), "experience_years": _num(0, 20), "score_points": _num(0, 100)},
    "paraphrase": {"enabled": ("bool",), "threshold": _num(0, 1), "max_lines": ("int", 0, 20),
                   "sections": ("sections",), "min_words": ("int", 1, 100), "max_words": ("int", 1, 200)},
    "validation": {"experience_tolerance_years": _num(0, 10), "equivalent_experience_years": ("number_or_none", 0, 40)},
    "score_threshold_rejects": ("bool",),
    "match_blocklist": ("blocklist",),
}


def _check(rule, value, path, problems):
    kind = rule[0]
    if kind in ("number", "number_or_none"):
        if value is None and kind == "number_or_none":
            return
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(f"{path}: give a number.")
        elif not rule[1] <= value <= rule[2]:
            problems.append(f"{path}: give a value from {rule[1]} to {rule[2]}.")
    elif kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            problems.append(f"{path}: give a whole number.")
        elif not rule[1] <= value <= rule[2]:
            problems.append(f"{path}: give a value from {rule[1]} to {rule[2]}.")
    elif kind == "bool":
        if not isinstance(value, bool):
            problems.append(f"{path}: give true or false.")
    elif kind == "sections":
        if not isinstance(value, list) or not value or not set(value) <= set(EVIDENCE_SECTION_OPTIONS):
            problems.append(f"{path}: give one or more of {', '.join(EVIDENCE_SECTION_OPTIONS)}.")
    elif kind == "blocklist":
        if not isinstance(value, list):
            problems.append(f"{path}: give a list of {{term, skill}} entries.")
            return
        for i, entry in enumerate(value):
            if not isinstance(entry, dict) or set(entry) - {"term", "skill", "note"}:
                problems.append(f"{path}[{i}]: give term, skill and an optional note.")
                continue
            for field in ("term", "skill"):
                text = entry.get(field)
                if not isinstance(text, str) or not 1 <= len(text.strip()) <= 120:
                    problems.append(f"{path}[{i}].{field}: give 1 to 120 characters.")
            if entry.get("note") is not None and (not isinstance(entry["note"], str) or len(entry["note"]) > 300):
                problems.append(f"{path}[{i}].note: up to 300 characters.")


def _describe(rule) -> dict:
    kind = rule[0]
    if kind in ("number", "number_or_none", "int"):
        return {"type": kind, "min": rule[1], "max": rule[2]}
    if kind == "sections":
        return {"type": "sections", "options": list(EVIDENCE_SECTION_OPTIONS)}
    return {"type": kind}


def schema_description() -> dict:
    """SCHEMA for the Settings page's version editor: {section: {key: {type, min?, max?, options?}} | {type}}."""
    return {section: ({key: _describe(r) for key, r in rule.items()} if isinstance(rule, dict) else _describe(rule))
            for section, rule in SCHEMA.items()}


def configuration_problems(configuration: dict) -> list[str]:
    """Every problem with these sections' values (unknown top-level keys are checked by the caller)."""
    problems = []
    for key, value in configuration.items():
        rule = SCHEMA.get(key)
        if rule is None:
            continue
        if isinstance(rule, dict):
            if not isinstance(value, dict):
                problems.append(f"{key}: give an object of {', '.join(rule)}.")
                continue
            for sub in sorted(set(value) - set(rule)):
                problems.append(f"{key}.{sub}: unknown setting (known: {', '.join(rule)}).")
            for sub, sub_rule in rule.items():
                if sub in value:
                    _check(sub_rule, value[sub], f"{key}.{sub}", problems)
        else:
            _check(rule, value, key, problems)
    paraphrase = configuration.get("paraphrase")
    if isinstance(paraphrase, dict) and isinstance(paraphrase.get("min_words"), int) \
            and isinstance(paraphrase.get("max_words"), int) and paraphrase["min_words"] > paraphrase["max_words"]:
        problems.append("paraphrase: min_words can't be more than max_words.")
    return problems
