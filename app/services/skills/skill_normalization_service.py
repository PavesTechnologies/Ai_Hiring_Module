import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from rapidfuzz import fuzz, process

from app.models.skills import JDSkillVerificationStatus, SkillOntology
from app.repositories.skill_repository import SEMANTIC_SIMILARITY_THRESHOLD, SkillRepository
from app.services.ai.embedding_service import EmbeddingService


class SkillMatchTier(str, Enum):
    EXACT = "EXACT"
    ALIAS = "ALIAS"
    CASE_INSENSITIVE = "CASE_INSENSITIVE"
    RULE_BASED = "RULE_BASED"
    FUZZY = "FUZZY"
    SEMANTIC = "SEMANTIC"
    UNKNOWN = "UNKNOWN"
    # Not produced by normalize_skills() itself — set directly by HR-driven
    # actions (JDSkill remap, unknown-skill mapping) via SkillRepository.
    MANUAL_HR = "MANUAL_HR"


# Tiers whose match is a deterministic string comparison are trusted
# outright; similarity-based tiers (fuzzy, semantic) are guesses and start
# out needing HR confirmation.
_AUTO_VERIFIED_TIERS = {
    SkillMatchTier.EXACT,
    SkillMatchTier.ALIAS,
    SkillMatchTier.CASE_INSENSITIVE,
    SkillMatchTier.RULE_BASED,
}


def verification_status_for_tier(tier: SkillMatchTier) -> JDSkillVerificationStatus:
    return (
        JDSkillVerificationStatus.AUTO_VERIFIED
        if tier in _AUTO_VERIFIED_TIERS
        else JDSkillVerificationStatus.PENDING_REVIEW
    )


@dataclass
class SkillMatchResult:
    raw_text: str
    mandatory: bool
    canonical_skill_id: UUID | None
    match_tier: SkillMatchTier
    confidence: float | None
    # Cleaned form used for matching (unicode/whitespace-normalized) — also
    # what gets stored as UnknownSkill.normalized_key when unmatched.
    normalized_text: str = ""


class SkillNormalizationService:
    """
    Matches raw JD skill strings against the skill ontology, in order:
    exact canonical -> exact alias -> case-insensitive canonical -> case-
    insensitive alias -> rule-based canonical -> rule-based alias ->
    fuzzy canonical -> semantic canonical -> unknown. Fuzzy and semantic
    search canonical names only — alias lookup is deterministic-tier only.
    Read-only: never mutates raw skill text, never discards an unmatched
    skill, never calls AI (embeddings are a local model, see EmbeddingService).
    """

    FUZZY_SCORE_THRESHOLD = 85.0

    def __init__(self, skill_repository: SkillRepository, embedding_service: EmbeddingService):
        self.skill_repository = skill_repository
        self.embedding_service = embedding_service

    def normalize_skills(self, required_skills: list[str], preferred_skills: list[str]) -> list[SkillMatchResult]:
        catalog = self.skill_repository.list_active_skills()
        results = [self._match_skill(raw, catalog, mandatory=True) for raw in required_skills]
        results.extend(self._match_skill(raw, catalog, mandatory=False) for raw in preferred_skills)
        return results

    def _match_skill(self, raw_text: str, catalog: list[SkillOntology], mandatory: bool) -> SkillMatchResult:
        normalized_text = self._normalize(raw_text)

        exact = self._match_exact(normalized_text, catalog)
        if exact:
            return SkillMatchResult(raw_text, mandatory, exact.id, SkillMatchTier.EXACT, 1.0, normalized_text)

        alias = self._match_alias(normalized_text, catalog)
        if alias:
            return SkillMatchResult(raw_text, mandatory, alias.id, SkillMatchTier.ALIAS, 1.0, normalized_text)

        case_insensitive = self._match_case_insensitive(normalized_text, catalog)
        if case_insensitive:
            return SkillMatchResult(
                raw_text, mandatory, case_insensitive.id, SkillMatchTier.CASE_INSENSITIVE, 1.0, normalized_text
            )

        rule_based = self._match_rule_based(normalized_text, catalog)
        if rule_based:
            return SkillMatchResult(
                raw_text, mandatory, rule_based.id, SkillMatchTier.RULE_BASED, 1.0, normalized_text
            )

        fuzzy_skill, fuzzy_score = self._match_fuzzy(normalized_text, catalog)
        if fuzzy_skill and fuzzy_score >= self.FUZZY_SCORE_THRESHOLD:
            return SkillMatchResult(
                raw_text, mandatory, fuzzy_skill.id, SkillMatchTier.FUZZY, fuzzy_score / 100, normalized_text
            )

        semantic_skill, similarity = self._match_semantic(normalized_text)
        if semantic_skill and similarity >= SEMANTIC_SIMILARITY_THRESHOLD:
            return SkillMatchResult(
                raw_text, mandatory, semantic_skill.id, SkillMatchTier.SEMANTIC, similarity, normalized_text
            )

        return SkillMatchResult(raw_text, mandatory, None, SkillMatchTier.UNKNOWN, None, normalized_text)

    @staticmethod
    def _normalize(raw_text: str) -> str:
        """
        Unicode/whitespace cleanup shared by every tier below. Deliberately
        does NOT lowercase here: Exact Canonical/Exact Alias need to stay
        case-sensitive, or Case Canonical/Case Alias — the very next tier —
        would never be reachable. Case-folding happens inside the case-
        insensitive/fuzzy/semantic comparisons themselves, same as today.
        """
        if not raw_text:
            return ""
        text = unicodedata.normalize("NFKC", raw_text).strip()
        return re.sub(r"\s+", " ", text)

    def _match_semantic(self, normalized_text: str) -> tuple[SkillOntology | None, float]:
        embedding = self.embedding_service.generate_embedding(normalized_text)
        result = self.skill_repository.find_by_embedding(embedding)
        if result is None:
            return None, 0.0
        return result

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
        # Canonical names only — fuzzy alias matching is intentionally not
        # part of the finalized pipeline (aliases are exact/case/rule only).
        choices: dict[str, SkillOntology] = {
            skill.canonical_name: skill for skill in catalog
        }

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
