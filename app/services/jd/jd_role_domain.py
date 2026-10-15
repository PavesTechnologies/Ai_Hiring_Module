"""
Suggested role domain for a JD (Phase 3 Step 2), shown in JD calibration;
HR confirms or changes it. The title decides when it names a domain (a
"Junior Python Developer" is SOFTWARE); otherwise the core skills, by the
most keyword hits. Nothing decided -> no suggestion (never guessed).
"""
import re
from dataclasses import dataclass

_KEYWORDS: dict[str, tuple[str, ...]] = {
    "SOFTWARE": ("developer", "software", "programmer", "backend", "back-end", "frontend", "front-end",
                 "full stack", "full-stack", "web", "mobile", "python", "java", "javascript", "typescript",
                 "react", "django", "fastapi", "node", ".net", "c#", "golang", "rest api", "sql", "git",
                 "devops", "sdet", "qa automation"),
    "IT_OPERATIONS": ("servicenow", "itsm", "itil", "service desk", "help desk", "helpdesk", "incident management",
                      "change management (itsm)", "service catalog", "cmdb", "system administrator",
                      "administrator", "infrastructure", "network", "it support", "it operations"),
    "DATA": ("data scientist", "data science", "data analyst", "data engineer", "machine learning", "analytics",
             "statistics", "power bi", "tableau", "etl"),
    "BUSINESS": ("transformation", "strategy", "program management", "programme management", "change management",
                 "stakeholder", "governance", "operating model", "consultant", "business analyst", "finance",
                 "marketing", "sales", "human resources"),
}


@dataclass(frozen=True)
class RoleDomainSuggestion:
    domain: str | None
    basis: str | None        # "title" / "skills" / None


def _hits(domain: str, text: str) -> int:
    return sum(1 for word in _KEYWORDS[domain] if re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", text))


def _decide(text: str) -> str | None:
    scores = sorted(((_hits(d, text), d) for d in _KEYWORDS), reverse=True)
    if scores[0][0] == 0 or scores[0][0] == scores[1][0]:
        return None
    return scores[0][1]


def suggest_role_domain(title: str | None, core_skill_names: list[str]) -> RoleDomainSuggestion:
    domain = _decide((title or "").lower())
    if domain:
        return RoleDomainSuggestion(domain, "title")
    domain = _decide(" | ".join(core_skill_names).lower())
    return RoleDomainSuggestion(domain, "skills" if domain else None)
