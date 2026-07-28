import logging
from uuid import UUID

from rapidfuzz import fuzz

from app.exception_handler.exceptions import ConflictError, NotFoundError
from app.models.skills import SkillOntology, UnknownSkill, UnknownSkillStatus
from app.repositories.config_repository import ConfigRepository
from app.repositories.skill_repository import SkillRepository
from app.schemas.unknown_skill.skill_suggestion_response import SkillSuggestionResponse
from app.services.ai.embedding_service import EmbeddingService

logger = logging.getLogger(__name__)

# A (skill, matched_alias, similarity) triple - matched_alias is None for
# canonical-tier candidates, the alias text for alias-tier candidates.
_ScoredCandidate = tuple[SkillOntology, str | None, float]


class UnknownSkillSuggestionService:
    """
    Generates ranked canonical-skill suggestions for an HR_ADMIN manually
    verifying a single Unknown Skill. The four search strategies (RapidFuzz
    / semantic x canonical / alias) share one fetch -> validate -> score ->
    threshold -> sort -> top-K pipeline; only candidate scoring differs.

    This is read-only and does not touch UnknownSkill.status or any
    verification/resolution workflow - it only surfaces candidates for a
    human to review.
    """

    TOP_K_CONFIG_KEY = "UNKNOWN_SKILL_SUGGESTION_TOP_K"
    RAPIDFUZZ_THRESHOLD_CONFIG_KEY = "UNKNOWN_SKILL_SUGGESTION_RAPIDFUZZ_THRESHOLD"
    SEMANTIC_THRESHOLD_CONFIG_KEY = "UNKNOWN_SKILL_SUGGESTION_SEMANTIC_THRESHOLD"

    DEFAULT_TOP_K = 10
    DEFAULT_RAPIDFUZZ_THRESHOLD = 85.0
    DEFAULT_SEMANTIC_THRESHOLD = 0.80

    def __init__(
        self,
        skill_repository: SkillRepository,
        config_repository: ConfigRepository,
        embedding_service: EmbeddingService,
    ):
        self.skill_repository = skill_repository
        self.config_repository = config_repository
        self.embedding_service = embedding_service

    # ── Public API - one method per endpoint ──────────────────────────────

    def get_rapidfuzz_canonical_suggestions(
        self, unknown_skill_id: UUID, *, limit: int | None, threshold: float | None
    ) -> list[SkillSuggestionResponse]:
        unknown_skill = self._get_pending_unknown_skill(unknown_skill_id)
        top_k, cutoff = self._resolve_params(limit, threshold, self.RAPIDFUZZ_THRESHOLD_CONFIG_KEY, self.DEFAULT_RAPIDFUZZ_THRESHOLD)

        candidates: list[_ScoredCandidate] = [
            (skill, None, float(fuzz.ratio(unknown_skill.raw_text, skill.canonical_name)))
            for skill in self.skill_repository.list_active_skills()
        ]
        top = self._rank_top_k(candidates, threshold=cutoff, top_k=top_k)
        logger.info(
            "RapidFuzz canonical suggestions | unknown_skill_id=%s candidates=%s returned=%s",
            unknown_skill_id, len(candidates), len(top),
        )
        return self._to_response(top)

    def get_semantic_canonical_suggestions(
        self, unknown_skill_id: UUID, *, limit: int | None, threshold: float | None
    ) -> list[SkillSuggestionResponse]:
        unknown_skill = self._get_pending_unknown_skill(unknown_skill_id)
        top_k, cutoff = self._resolve_params(limit, threshold, self.SEMANTIC_THRESHOLD_CONFIG_KEY, self.DEFAULT_SEMANTIC_THRESHOLD)

        target_embedding = self.embedding_service.generate_embedding(unknown_skill.raw_text)
        nearest = self.skill_repository.find_top_similar_canonical_skills(target_embedding, limit=top_k)
        candidates: list[_ScoredCandidate] = [(skill, None, float(similarity)) for skill, similarity in nearest]
        top = self._rank_top_k(candidates, threshold=cutoff, top_k=top_k)
        logger.info(
            "Semantic canonical suggestions | unknown_skill_id=%s candidates=%s returned=%s",
            unknown_skill_id, len(candidates), len(top),
        )
        return self._to_response(top)

    def get_rapidfuzz_alias_suggestions(
        self, unknown_skill_id: UUID, *, limit: int | None, threshold: float | None
    ) -> list[SkillSuggestionResponse]:
        unknown_skill = self._get_pending_unknown_skill(unknown_skill_id)
        top_k, cutoff = self._resolve_params(limit, threshold, self.RAPIDFUZZ_THRESHOLD_CONFIG_KEY, self.DEFAULT_RAPIDFUZZ_THRESHOLD)

        candidates: list[_ScoredCandidate] = [
            (skill, alias, float(fuzz.ratio(unknown_skill.raw_text, alias)))
            for skill, alias in self.skill_repository.get_all_skill_aliases()
        ]
        top = self._rank_top_k(candidates, threshold=cutoff, top_k=top_k)
        logger.info(
            "RapidFuzz alias suggestions | unknown_skill_id=%s candidates=%s returned=%s",
            unknown_skill_id, len(candidates), len(top),
        )
        return self._to_response(top)

    def get_semantic_alias_suggestions(
        self, unknown_skill_id: UUID, *, limit: int | None, threshold: float | None
    ) -> list[SkillSuggestionResponse]:
        unknown_skill = self._get_pending_unknown_skill(unknown_skill_id)
        top_k, cutoff = self._resolve_params(limit, threshold, self.SEMANTIC_THRESHOLD_CONFIG_KEY, self.DEFAULT_SEMANTIC_THRESHOLD)

        target_embedding = self.embedding_service.generate_embedding(unknown_skill.raw_text)
        candidates: list[_ScoredCandidate] = [
            (skill, alias, self._cosine_similarity(target_embedding, self.embedding_service.generate_embedding(alias)))
            for skill, alias in self.skill_repository.get_all_skill_aliases()
        ]
        top = self._rank_top_k(candidates, threshold=cutoff, top_k=top_k)
        logger.info(
            "Semantic alias suggestions | unknown_skill_id=%s candidates=%s returned=%s",
            unknown_skill_id, len(candidates), len(top),
        )
        return self._to_response(top)

    # ── Validation ──────────────────────────────────────────────────────────

    def _get_pending_unknown_skill(self, unknown_skill_id: UUID) -> UnknownSkill:
        unknown_skill = self.skill_repository.get_unknown_skill_by_id(unknown_skill_id)
        if unknown_skill is None:
            raise NotFoundError(f"Unknown skill '{unknown_skill_id}' was not found.")
        if unknown_skill.status != UnknownSkillStatus.PENDING:
            raise ConflictError(
                f"Unknown skill '{unknown_skill_id}' is not pending verification "
                f"(status={unknown_skill.status.value})."
            )
        return unknown_skill

    # ── Shared scoring helpers ────────────────────────────────────────────

    def _resolve_params(
        self,
        limit: int | None,
        threshold: float | None,
        threshold_config_key: str,
        default_threshold: float,
    ) -> tuple[int, float]:
        """limit/threshold from the request take priority; otherwise fall back to configured defaults."""
        configs = self.config_repository.get_configs_by_keys([self.TOP_K_CONFIG_KEY, threshold_config_key])

        top_k = limit if limit is not None else self._parse_int(configs.get(self.TOP_K_CONFIG_KEY), self.DEFAULT_TOP_K)
        cutoff = threshold if threshold is not None else self._parse_float(configs.get(threshold_config_key), default_threshold)
        return top_k, cutoff

    @staticmethod
    def _rank_top_k(
        candidates: list[_ScoredCandidate], *, threshold: float, top_k: int
    ) -> list[_ScoredCandidate]:
        """
        Sort descending by similarity, keep only candidates clearing the
        threshold, and cap at top_k. Fallback strategy: if nothing clears
        the threshold, return the highest-ranked top_k anyway so the HR
        reviewer always has candidates to look at instead of an empty list.
        """
        ranked = sorted(candidates, key=lambda candidate: candidate[2], reverse=True)
        passing = [candidate for candidate in ranked if candidate[2] >= threshold]
        return (passing or ranked)[:top_k]

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """
        Aliases have no persisted embedding column (they live inline in
        SkillOntology.aliases, not a separate embedded table), so unlike the
        canonical tier's pgvector query this compares embeddings computed
        on demand via EmbeddingService. Both inputs are already
        L2-normalized by generate_embedding, so a plain dot product equals
        cosine similarity.
        """
        return float(sum(x * y for x, y in zip(a, b)))

    @staticmethod
    def _to_response(candidates: list[_ScoredCandidate]) -> list[SkillSuggestionResponse]:
        return [
            SkillSuggestionResponse(
                skill_id=skill.id,
                skill_name=skill.canonical_name,
                matched_alias=matched_alias,
                similarity=round(similarity, 2),
            )
            for skill, matched_alias, similarity in candidates
        ]

    @staticmethod
    def _parse_int(raw: str | None, default: int) -> int:
        if not raw:
            return default
        try:
            return int(float(raw))
        except ValueError:
            return default

    @staticmethod
    def _parse_float(raw: str | None, default: float) -> float:
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            return default
