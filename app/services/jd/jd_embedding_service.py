import logging
from uuid import UUID

from app.models.jd.job_descriptions import JDEmbedding
from app.repositories.config_repository import ConfigRepository
from app.repositories.jd_repository import JDRepository
from app.repositories.skill_repository import SkillRepository
from app.services.ai.embedding_service import EmbeddingService
from app.services.jd.hash_service import HashService
from app.services.jd.jd_embedding_text_builder import build_jd_embedding_text

logger = logging.getLogger(__name__)

# Not yet seeded before this story - reused as-is once present; falls back
# to the module default below if missing/invalid, never hard-fails.
JD_EMBEDDING_MAX_CHARS_KEY = "JD_EMBEDDING_MAX_CHARS"
_DEFAULT_JD_EMBEDDING_MAX_CHARS = 2000


class JDEmbeddingService:
    """
    M08-E01 S02 Phase 1: Backend Foundation for JD Embedding Generation.

    Builds the canonical embedding text from a JD's already-persisted
    title/raw_text plus its jd_skills (joined to skill_ontology for
    canonical_name - never raw, pre-normalization skill strings), reuses
    EmbeddingService (the exact same model/service used for resume
    embeddings - never a second embedding provider) to generate a
    VECTOR(384) only when no existing jd_embeddings row already has an
    identical content hash for the active embedding model version, and
    persists the result via JDRepository.

    Deliberately NOT wired to Celery yet (out of scope for this phase) -
    a plain, directly-callable service, same shape as
    SemanticScoringService/CandidateScoringService: takes an id, does its
    own fetches, does not commit (that's the caller's responsibility, once
    a future phase wires this into a task or route). Does not touch the
    existing inline EMBEDDING_GENERATION pipeline stage
    (app/services/jd/jd_processing_pipeline.py) or its own text builder
    (EmbeddingService.build_canonical_embedding_text) at all - that flow
    keeps working exactly as before, unchanged.
    """

    def __init__(
        self,
        jd_repository: JDRepository,
        skill_repository: SkillRepository,
        config_repository: ConfigRepository,
        embedding_service: EmbeddingService,
    ):
        self.jd_repository = jd_repository
        self.skill_repository = skill_repository
        self.config_repository = config_repository
        self.embedding_service = embedding_service

    def generate_and_store_embedding(self, jd_id: UUID, force_regenerate: bool = False) -> JDEmbedding:
        """
        force_regenerate=False (default - the "JD activated" trigger):
        if a jd_embeddings row already exists for this jd_id, returns it
        unchanged without rebuilding the text/hash or touching the
        embedding model at all - activation doesn't imply anything about
        this JD's content actually changed. Used for JD create/reprocess
        (where the existing inline pipeline stage already embeds the JD
        before this ever runs - a guaranteed no-op there) and for a
        metadata-only JD update (a brand-new jd_id with no row yet, so
        this generates one for the first time).

        force_regenerate=True (the "jd_skills changed" trigger): always
        rebuilds the text/hash from this JD's current jd_skills - even if
        a row already exists - and, when the content actually changed,
        overwrites that existing row in place via
        JDRepository.replace_jd_embedding (jd_id is unique - there is only
        ever one row per JD version to update). Still skips calling the
        embedding model itself if the freshly-built text's hash matches
        some other existing jd_embeddings row (content_hash dedup).
        """
        job_description = self.jd_repository.get_by_id(jd_id)
        if job_description is None:
            raise ValueError(f"JobDescription '{jd_id}' not found.")

        if not force_regenerate:
            existing_for_jd = self.jd_repository.get_embedding_by_jd_id(jd_id)
            if existing_for_jd is not None:
                logger.info(
                    "JD embedding already exists for jd_id=%s - skipping (not a skill-triggered regeneration).",
                    jd_id,
                )
                return existing_for_jd

        mandatory_names, preferred_names = self._resolve_skill_names(jd_id)
        max_chars = self._read_max_chars()

        embedding_text = build_jd_embedding_text(
            title=job_description.title,
            raw_text=job_description.raw_text,
            mandatory_skill_names=mandatory_names,
            preferred_skill_names=preferred_names,
            max_chars=max_chars,
        )
        content_hash = HashService.generate_hash(embedding_text)

        # Active model only, never hardcoded - raises RuntimeError if none
        # is configured (an infra/config problem, not this JD's fault).
        embedding_model_version = self.jd_repository.get_active_embedding_model_version()

        # Dedup check before ever calling the embedding service.
        existing = self.jd_repository.get_embedding_by_content_hash(
            content_hash, embedding_model_version.id,
        )
        if existing is not None:
            vector = existing.embedding
            logger.info(
                "Reusing existing JD embedding vector for jd_id=%s (content_hash match, source jd_embedding_id=%s)",
                jd_id, existing.id,
            )
        else:
            vector = self.embedding_service.generate_embedding(embedding_text)
            logger.info("Generated new JD embedding for jd_id=%s", jd_id)

        if force_regenerate:
            return self.jd_repository.replace_jd_embedding(
                jd_id=jd_id,
                embedding=vector,
                embedding_model_version_id=embedding_model_version.id,
                content_hash=content_hash,
            )

        jd_embedding, was_created = self.jd_repository.create_jd_embedding_idempotent(
            jd_id=jd_id,
            embedding=vector,
            embedding_model_version_id=embedding_model_version.id,
            content_hash=content_hash,
        )
        if not was_created:
            logger.warning(
                "jd_embeddings row already existed for jd_id=%s - returned the existing row unchanged, "
                "no update performed.", jd_id,
            )
        return jd_embedding

    def _resolve_skill_names(self, jd_id: UUID) -> tuple[list[str], list[str]]:
        """
        Reuses SkillRepository.get_jd_skills_by_jd_id (already joins
        jd_skills to skill_ontology) - never a new/duplicate query. Splits
        client-side by JDSkill.mandatory into the two canonical_name lists
        the embedding text needs.
        """
        rows = self.skill_repository.get_jd_skills_by_jd_id(jd_id)
        mandatory_names = [ontology.canonical_name for jd_skill, ontology in rows if jd_skill.mandatory]
        preferred_names = [ontology.canonical_name for jd_skill, ontology in rows if not jd_skill.mandatory]
        return mandatory_names, preferred_names

    def _read_max_chars(self) -> int:
        raw = self.config_repository.get_configs_by_keys([JD_EMBEDDING_MAX_CHARS_KEY]).get(
            JD_EMBEDDING_MAX_CHARS_KEY,
        )
        if raw is None:
            return _DEFAULT_JD_EMBEDDING_MAX_CHARS
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.warning(
                "Invalid JD_EMBEDDING_MAX_CHARS platform_config value %r - falling back to default %s.",
                raw, _DEFAULT_JD_EMBEDDING_MAX_CHARS,
            )
            return _DEFAULT_JD_EMBEDDING_MAX_CHARS
