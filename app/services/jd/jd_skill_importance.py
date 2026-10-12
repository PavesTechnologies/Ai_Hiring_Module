"""
A required JD skill's core/supporting importance, read back from the JD's
own extraction (extracted_json.required_skills.core / .supporting, plus
their aliases).

JD processing classifies every required skill, but a skill that was unknown
to the catalog at upload only gets its jd_skills row later - when HR
approves it (MANUAL_HR). That row used to be created with no importance, and
a JD whose core skills are all unclassified silently loses its core-skill
gate (observed: 10 of 11 skills NULL, "100% core coverage" while every core
skill was missing). This recovers the classification the extraction already
made - it never guesses.
"""
import re

from app.models.skills import JDSkillImportance

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _key(name: str | None) -> str:
    return _NON_ALNUM.sub(" ", (name or "").lower()).strip()


def importance_from_extraction(extracted_json: dict | None, *names: str | None) -> JDSkillImportance | None:
    """CORE/SUPPORTING for the first of `names` the JD's extraction lists as required (by name or alias), else None."""
    required = (extracted_json or {}).get("required_skills") or {}
    aliases = (extracted_json or {}).get("aliases") or {}
    wanted = {_key(n) for n in names if _key(n)}
    if not wanted or not isinstance(required, dict):
        return None
    for importance, label in ((JDSkillImportance.CORE, "core"), (JDSkillImportance.SUPPORTING, "supporting")):
        for skill in required.get(label) or []:
            candidates = {_key(skill)} | {_key(a) for a in (aliases.get(skill) or [])}
            if candidates & wanted:
                return importance
    return None
