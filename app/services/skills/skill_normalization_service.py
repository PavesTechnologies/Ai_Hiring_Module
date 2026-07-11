import re
from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from rapidfuzz import fuzz, process

from app.models.skills import SkillOntology
from app.repositories.skill_repository import SkillRepository


class SkillMatchTier(str, Enum):
    EXACT = "EXACT"
    ALIAS = "ALIAS"
    CASE_INSENSITIVE = "CASE_INSENSITIVE"
    RULE_BASED = "RULE_BASED"
    FUZZY = "FUZZY"
    SEMANTIC = "SEMANTIC"
    UNKNOWN = "UNKNOWN"


@dataclass
class SkillMatchResult:
    raw_text: str
    mandatory: bool
    canonical_skill_id: UUID | None
    match_tier: SkillMatchTier
    confidence: float | None


class SkillNormalizationService:
    """
    Matches raw JD skill strings against the skill ontology, in order:
    exact -> alias -> case-insensitive -> rule-based -> RapidFuzz ->
    semantic (deferred) -> unknown. Read-only: never mutates raw skill
    text, never discards an unmatched skill, never calls AI.
    """

    FUZZY_SCORE_THRESHOLD = 85.0

    def __init__(self, skill_repository: SkillRepository):
        self.skill_repository = skill_repository

    def normalize_skills(self, required_skills: list[str], preferred_skills: list[str]) -> list[SkillMatchResult]:
        catalog = self.skill_repository.list_active_skills()
        results = [self._match_skill(raw, catalog, mandatory=True) for raw in required_skills]
        results.extend(self._match_skill(raw, catalog, mandatory=False) for raw in preferred_skills)
        return results

    def _match_skill(self, raw_text: str, catalog: list[SkillOntology], mandatory: bool) -> SkillMatchResult:
        exact = self._match_exact(raw_text, catalog)
        if exact:
            return SkillMatchResult(raw_text, mandatory, exact.id, SkillMatchTier.EXACT, 1.0)

        alias = self._match_alias(raw_text, catalog)
        if alias:
            return SkillMatchResult(raw_text, mandatory, alias.id, SkillMatchTier.ALIAS, 1.0)

        case_insensitive = self._match_case_insensitive(raw_text, catalog)
        if case_insensitive:
            return SkillMatchResult(raw_text, mandatory, case_insensitive.id, SkillMatchTier.CASE_INSENSITIVE, 1.0)

        rule_based = self._match_rule_based(raw_text, catalog)
        if rule_based:
            return SkillMatchResult(raw_text, mandatory, rule_based.id, SkillMatchTier.RULE_BASED, 1.0)

        fuzzy_skill, fuzzy_score = self._match_fuzzy(raw_text, catalog)
        if fuzzy_skill and fuzzy_score >= self.FUZZY_SCORE_THRESHOLD:
            return SkillMatchResult(raw_text, mandatory, fuzzy_skill.id, SkillMatchTier.FUZZY, fuzzy_score / 100)

        # Semantic (embedding-similarity) matching is explicitly deferred.
        return SkillMatchResult(raw_text, mandatory, None, SkillMatchTier.UNKNOWN, None)

    @staticmethod
    def _match_exact(raw_text: str, catalog: list[SkillOntology]) -> SkillOntology | None:
        return next((skill for skill in catalog if skill.canonical_name == raw_text), None)

    @staticmethod
    def _match_alias(raw_text: str, catalog: list[SkillOntology]) -> SkillOntology | None:
        return next((skill for skill in catalog if raw_text in (skill.aliases or [])), None)

    @staticmethod
    def _match_case_insensitive(raw_text: str, catalog: list[SkillOntology]) -> SkillOntology | None:
        lowered = raw_text.lower()
        for skill in catalog:
            if skill.canonical_name.lower() == lowered:
                return skill
            if any(alias.lower() == lowered for alias in (skill.aliases or [])):
                return skill
        return None

    @classmethod
    def _match_rule_based(cls, raw_text: str, catalog: list[SkillOntology]) -> SkillOntology | None:
        normalized = cls._rule_normalize(raw_text)
        for skill in catalog:
            if cls._rule_normalize(skill.canonical_name) == normalized:
                return skill
            if any(cls._rule_normalize(alias) == normalized for alias in (skill.aliases or [])):
                return skill
        return None

    @staticmethod
    def _rule_normalize(text: str) -> str:
        normalized = text.lower().strip()
        normalized = re.sub(r"[.\-_/]+", " ", normalized)
        return re.sub(r"\s+", " ", normalized).strip()

    @staticmethod
    def _match_fuzzy(raw_text: str, catalog: list[SkillOntology]) -> tuple[SkillOntology | None, float]:
        choices: dict[str, SkillOntology] = {}
        for skill in catalog:
            choices.setdefault(skill.canonical_name, skill)
            for alias in (skill.aliases or []):
                choices.setdefault(alias, skill)

        if not choices:
            return None, 0.0

        # Plain ratio (not WRatio): WRatio's partial-ratio component scores
        # substring pairs like "Java" vs "JavaScript" as a near-match, which
        # is a false positive here — these must stay distinct skills.
        match = process.extractOne(raw_text, choices.keys(), scorer=fuzz.ratio)
        if not match:
            return None, 0.0

        matched_text, score, _ = match
        return choices[matched_text], score
