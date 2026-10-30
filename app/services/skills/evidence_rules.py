"""
Scoring v2 evidence rules (Phase 3 hard negatives) - config
SCORING_V2_CONFIG["evidence"], defaults below.

- qualifiers: a resume line holding one ("currently learning", "familiar
  with", ...) is not evidence for any skill on it, from any source.
  Catalogue skill names are taken out of the line first, so "Machine
  Learning, Python" is not a "learning" line.
- work verbs: an exact match auto-confirms only on a line that leads with
  the work - its first verb is a work verb ("built", "configured", ...) -
  and none of the not-work verbs appears ("sold", "demoed", "supported",
  "attended", ...). A work verb later in the line ("Tested REST endpoints
  using Postman") doesn't count. Anything else goes to the 3-vote confirmation.
- role: the job title and company of the work-history role a resume line
  belongs to, shown to the voters with the excerpt.

Words are compared in term_key form (lower case, punctuation dropped,
American spelling), so "customised" and "customized" are one word.
"""
from dataclasses import dataclass

from app.services.skills.resume_sections import heading_section
from app.services.skills.resume_skill_scanner import squash_words, term_key

DEFAULT_EVIDENCE_RULES = {
    "qualifiers": ["currently learning", "learning", "familiar with", "basic knowledge", "exposure to",
                   "interested in", "aspiring"],
    "work_verbs": [
        "built", "build", "builds", "building", "configured", "configure", "configures", "configuring",
        "implemented", "implement", "implements", "implementing", "developed", "develop", "develops", "developing",
        "administered", "administer", "administers", "administering", "wrote", "write", "writes", "writing",
        "written", "designed", "design", "designs", "designing", "used", "use", "uses", "using",
        "maintained", "maintain", "maintains", "maintaining", "created", "create", "creates", "creating",
        "deployed", "deploy", "deploys", "deploying", "automated", "automate", "automating",
        "integrated", "integrate", "integrating", "migrated", "migrate", "migrating",
        "customised", "customise", "customising", "scripted", "coded", "programmed", "engineered",
    ],
    "non_work_verbs": [
        "sold", "sell", "sells", "selling", "demo", "demos", "demoed", "demonstrated", "demonstrating", "presales",
        "supports", "supported", "supporting", "attended", "attend", "attends", "attending",
        # advisory work: advising on a skill is not doing it (the senior test set, S6)
        "advised", "advise", "advises", "advising", "assessed", "assess", "assesses", "assessing",
        "recommended", "recommend", "recommends", "recommending", "evaluated", "evaluate", "evaluates", "evaluating",
        "consulted", "consult", "consults", "consulting",
    ],
}


# Verbs a resume line often leads with that are neither work nor not-work verbs - the line's
# first verb then isn't a work verb, so it goes to the vote. Regular past tenses ("tested",
# "managed") are caught by their "-ed" ending; these are the ones that aren't.
OTHER_VERBS = frozenset({
    "led", "lead", "leads", "leading", "ran", "run", "runs", "running", "drove", "drive", "drives", "driving",
    "oversaw", "oversee", "oversees", "overseeing", "own", "owns", "owning", "made", "make", "makes", "making",
    "set", "sets", "setting", "took", "take", "takes", "taking", "grew", "grow", "grows", "growing",
    "taught", "teach", "teaches", "teaching", "manage", "manages", "managing", "test", "tests", "testing",
    "work", "works", "working", "help", "helps", "helping", "assist", "assists", "assisting",
    "handle", "handles", "handling", "deliver", "delivers", "delivering", "coordinate", "coordinates",
    "coordinating", "review", "reviews", "reviewing", "train", "trains", "training", "mentor", "mentors",
    "mentoring", "monitor", "monitors", "monitoring", "troubleshoot", "troubleshoots", "troubleshooting",
    "troubleshot", "resolve", "resolves", "resolving", "participate", "participates", "participating",
    "contribute", "contributes", "contributing", "collaborate", "collaborates", "collaborating",
    "won", "win", "wins", "spoke", "speak", "speaks", "speaking",
})
# "-ed" words that are not past-tense verbs where a line starts with a noun phrase.
NOT_VERBS = frozenset({"speed", "embedded", "distributed", "advanced", "based", "related", "detailed",
                       "dedicated", "limited", "certified", "skilled", "experienced", "interested",
                       "seasoned", "talented", "focused", "oriented", "hundred"})


def _keys(values) -> list[str]:
    return [k for k in (term_key(str(v)) for v in values or []) if k]


@dataclass(frozen=True)
class EvidenceRules:
    qualifiers: tuple[str, ...]
    work_verbs: frozenset
    non_work_verbs: frozenset
    # Catalogue terms holding a qualifier word ("machine learning") - taken out before the check.
    masked_terms: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, config: dict | None, catalog_terms=()) -> "EvidenceRules":
        cfg = {**DEFAULT_EVIDENCE_RULES, **{k: v for k, v in (config or {}).items() if isinstance(v, list)}}
        qualifiers = tuple(sorted(set(_keys(cfg["qualifiers"])), key=len, reverse=True))
        words = {w for q in qualifiers for w in q.split()}
        masked = {t for t in (term_key(c) for c in catalog_terms) if t and t not in qualifiers and set(t.split()) & words}
        return cls(qualifiers, frozenset(_keys(cfg["work_verbs"])), frozenset(_keys(cfg["non_work_verbs"])),
                   tuple(sorted(masked, key=len, reverse=True)))

    def qualified(self, line: str | None) -> bool:
        """The line holds a qualifier outside any catalogue skill name."""
        text = squash_words(line or "")
        for term in self.masked_terms:
            needle = f" {term} "
            while needle in text:
                text = text.replace(needle, " ")
        return any(f" {q} " in text for q in self.qualifiers)

    def does_the_work(self, line: str | None) -> bool:
        """A work verb on the line and no not-work verb."""
        words = set(term_key(line or "").split())
        return bool(words & self.work_verbs) and not (words & self.non_work_verbs)

    def leading_verb(self, line: str | None) -> str | None:
        """
        The line's first verb: the first word that is a work / not-work verb, another common
        resume verb ("led", "ran"), or a past-tense "-ed" word ("tested"). None when there is none.
        """
        for word in term_key(line or "").split():
            if (word in self.work_verbs or word in self.non_work_verbs or word in OTHER_VERBS
                    or (len(word) >= 5 and word.endswith("ed") and word not in NOT_VERBS)):
                return word
        return None

    def leads_with_the_work(self, line: str | None) -> bool:
        """
        What auto-confirms: the line's first verb is a work verb and no not-work verb appears.
        "Built the ML platform" does; "Tested REST endpoints manually using Postman" does not -
        a later verb ("using") never counts, so that line goes to the vote.
        """
        return self.leading_verb(line) in self.work_verbs and not self.not_doing_the_work(line)

    def not_doing_the_work(self, line: str | None) -> bool:
        """A not-work verb on the line ("sold", "demoed", "attended", "supported", ...)."""
        return bool(set(term_key(line or "").split()) & self.non_work_verbs)


DEFAULT_RULES = EvidenceRules.from_config(None)


def role_label(role: dict) -> str | None:
    parts = [str(role.get(k)).strip() for k in ("title", "company") if role.get(k) and str(role.get(k)).strip()]
    return ", ".join(parts) or None


def role_for_line(lines: list[str], line_no: int, roles: list[dict] | None) -> str | None:
    """
    "Title, Company" of the work-history role (parsed work_experience) a
    resume line belongs to: the role whose description holds the line,
    else the nearest line above (within its section) naming a role's title
    or company. None when no role is found.
    """
    if not roles or line_no is None or not (0 <= line_no < len(lines)):
        return None
    key = term_key(lines[line_no])
    if len(key.split()) >= 3:
        for role in roles:
            if key in term_key(role.get("description") or ""):
                return role_label(role)
    companies = [term_key(role.get("company") or "") for role in roles]
    for i in range(line_no, -1, -1):
        if i != line_no and heading_section(lines[i]):
            break
        line = squash_words(lines[i])
        # A title names one role; a company only when no other role shares it.
        titled = [role for role in roles if (t := term_key(role.get("title") or "")) and f" {t} " in line]
        if titled:
            return role_label(max(titled, key=lambda r: len(term_key(r.get("title") or ""))))
        for role, company in zip(roles, companies):
            if company and f" {company} " in line and companies.count(company) == 1:
                return role_label(role)
    return None


def roles_of(parsed) -> list[dict]:
    """Work-history roles (title, company, description) from a parse (parsed_json dict or response object)."""
    entries = parsed.get("work_experience") if isinstance(parsed, dict) else getattr(parsed, "work_experience", None)
    roles = []
    for entry in entries or []:
        get = entry.get if isinstance(entry, dict) else (lambda k, e=entry: getattr(e, k, None))
        roles.append({"title": get("title"), "company": get("company"), "description": get("description")})
    return roles
