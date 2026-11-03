"""
Test fixtures in v2's stored breakdown shape (retire v1, phase C: every stored deterministic breakdown
is scoring_v2.score_v2's). with_v2_checks() completes a fixture written with just the parts a test is
about - its mandatory skills, experience, education - the way score_v2 would: core entries with their
gate credit, the checks, the failed checks, the outcome.
"""


def with_v2_checks(detail: dict | None, *, passed: bool | None = None) -> dict:
    b = dict(detail or {})
    skills = []
    for entry in b.get("mandatory_skills") or []:
        entry = dict(entry)
        entry.setdefault("importance", "core")
        entry.setdefault("counts_for_core_gate", entry.get("match_type") != "MISSING")
        entry.setdefault("confirmation_status", "CONFIRMED" if entry["counts_for_core_gate"]
                         else ("NOT_FOUND" if entry.get("match_type") == "MISSING" else "UNCERTAIN"))
        skills.append(entry)
    core = [e for e in skills if e["importance"] == "core"]
    unconfirmed = [e.get("canonical_name") for e in core if not e["counts_for_core_gate"]]
    experience = b.get("experience_validation") or {}
    education = b.get("education_validation") or {}
    checks = [
        {"check": "Core skills", "passed": not unconfirmed, "applies": bool(core),
         "detail": f"{len(core) - len(unconfirmed)} of {len(core)} core skills confirmed"
                   + (f" - missing: {', '.join(map(str, unconfirmed))}" if unconfirmed else "")},
        {"check": "Experience", "passed": experience.get("passed", True) is not False,
         "applies": bool(experience), "detail": "experience"},
        {"check": "Education", "passed": education.get("passed", True) is not False,
         "applies": bool(education), "detail": "education"},
    ]
    failed = [c["check"] for c in checks if not c["passed"]]
    decided = not failed if passed is None else passed
    b.update(mandatory_skills=skills, checks=checks, failed_checks=failed, scoring_version="v2",
             scoring_mode=b.get("scoring_mode", "llm"),
             outcome=b.get("outcome") or ("PASS" if decided else "REJECT"),
             deterministic_passed=b.get("deterministic_passed", decided),
             core_skill_count=len(core), core_confirmed_count=len(core) - len(unconfirmed),
             missing_core_skill_count=len(unconfirmed))
    return b
