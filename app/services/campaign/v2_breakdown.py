"""
Reading a stored deterministic breakdown, which is v2's own breakdown since retire v1, phase C
(scoring_v2.score_v2's shape; migration d8f0a2c4e6b9 moved the stored ones).

One older shape still exists in history only: a v2 decision stored v1-shaped, with v2's breakdown
nested under "v2" (acted_on_version "v2") - stage-history snapshots taken before phase C. as_v2()
reads the v2 breakdown out of it, so analytics and exports read both alike. A fresh database never
has that shape.
"""

# The v2 checks that are about skills (scoring_v2.score_v2 "checks").
SKILL_CHECKS = ("Core skills", "Core skill coverage", "Required skill coverage")


def as_v2(breakdown: dict | None) -> dict:
    """The v2 breakdown: the stored one, or the one nested in a pre-phase-C v1-shaped wrapper."""
    if not isinstance(breakdown, dict):
        return {}
    nested = breakdown.get("v2")
    if breakdown.get("acted_on_version") == "v2" and isinstance(nested, dict):
        return nested
    return breakdown


def unconfirmed_core_entries(breakdown: dict | None) -> list[dict]:
    """Core skill entries that didn't count toward the core gate - missing, unconfirmed, voted against."""
    b = as_v2(breakdown)
    return [e for e in b.get("mandatory_skills") or []
            if e.get("importance") == "core" and not e.get("counts_for_core_gate")]


def unconfirmed_core_skills(breakdown: dict | None) -> list[str]:
    return [str(e.get("canonical_name") or e.get("canonical_skill_id")) for e in unconfirmed_core_entries(breakdown)]


def failed_checks(breakdown: dict | None) -> list[str]:
    b = as_v2(breakdown)
    return [c["check"] for c in b.get("checks") or [] if not c.get("passed")]


def skills_check_failed(breakdown: dict | None) -> bool:
    return any(check in SKILL_CHECKS for check in failed_checks(breakdown))
