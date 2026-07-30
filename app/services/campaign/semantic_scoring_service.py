import logging
from datetime import datetime, timezone
from uuid import UUID

from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.jd_repository import JDRepository
from app.repositories.resume_repository import ResumeRepository

logger = logging.getLogger(__name__)

# Already seeded (app/seeds/seed_platform_config.py) - reused as-is, never a
# second/duplicated threshold key.
SEMANTIC_PASS_THRESHOLD_KEY = "SEMANTIC_PASS_THRESHOLD"
_DEFAULT_SEMANTIC_PASS_THRESHOLD = 0.65


class MissingResumeEmbeddingError(Exception):
    """
    The candidate's resume has no embedding yet. Deliberately NOT a
    ValueError/KeyError/TypeError - error_classifier.classify() would
    otherwise mark this PERMANENT; the embedding may simply not have
    finished generating yet (EMBED_RESUME is async and independent of this
    flow), so it must be retryable.
    """


class MissingJDEmbeddingError(Exception):
    """Same reasoning as MissingResumeEmbeddingError, for the JD side."""


class SemanticScoringService:
    """
    M08-E02: computes semantic similarity between an already-generated
    resume embedding (M08-E01's EMBED_RESUME) and an already-generated JD
    embedding (the JD processing pipeline) - never regenerates either.
    Mirrors CandidateScoringService.calculate_and_store_score_breakdown's
    shape exactly: takes IDs, fetches what it needs via injected
    repositories, builds a breakdown dict, flushes via
    CampaignCandidateRepository.update() but does not commit - that belongs
    to the caller (the Celery task), same as the deterministic service.
    """

    def __init__(
        self,
        resume_repository: ResumeRepository,
        jd_repository: JDRepository,
        config_repository: ConfigRepository,
        campaign_candidate_repository: CampaignCandidateRepository,
    ):
        self.resume_repository = resume_repository
        self.jd_repository = jd_repository
        self.config_repository = config_repository
        self.campaign_candidate_repository = campaign_candidate_repository

    def calculate_and_store_semantic_score_breakdown(
        self,
        campaign_candidate_id: UUID,
        jd_id: UUID,
        resume_id: UUID,
    ) -> dict:
        campaign_candidate = self.campaign_candidate_repository.get_by_id(campaign_candidate_id)
        if campaign_candidate is None:
            raise ValueError(f"CampaignCandidate '{campaign_candidate_id}' not found.")

        resume_embedding = self.resume_repository.get_embedding(resume_id)
        if resume_embedding is None:
            raise MissingResumeEmbeddingError(
                f"Resume '{resume_id}' has no embedding yet - semantic scoring cannot run."
            )

        jd_embedding = self.jd_repository.get_embedding_by_jd_id(jd_id)
        if jd_embedding is None:
            raise MissingJDEmbeddingError(
                f"Job description '{jd_id}' has no embedding yet - semantic scoring cannot run."
            )

        # Task 3: pgvector's own cosine_distance comparator (see
        # ResumeRepository.get_cosine_similarity) - never a manual Python
        # dot-product/norm calculation.
        similarity = self.resume_repository.get_cosine_similarity(resume_embedding.id, jd_embedding.embedding)
        if similarity is None:
            raise MissingResumeEmbeddingError(
                f"Resume embedding '{resume_embedding.id}' could not be compared - "
                "it may have been deleted concurrently."
            )

        threshold = self._read_threshold()
        passed = similarity >= threshold

        matching_skills, missing_skills = self._skills_from_deterministic_breakdown(campaign_candidate)
        matched_keywords = self._matched_keywords(jd_id, resume_id)

        breakdown = {
            "semantic_score": round(similarity, 6),
            "overall_similarity": round(similarity, 6),
            "semantic_passed": passed,
            "semantic_threshold": threshold,
            "matching_skills": matching_skills,
            "missing_skills": missing_skills,
            "matched_keywords": matched_keywords,
            "semantic_explanation": self._build_explanation(similarity, threshold, passed),
            "resume_embedding_model_version_id": str(resume_embedding.embedding_model_version_id),
            "jd_embedding_model_version_id": str(jd_embedding.embedding_model_version_id),
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }

        campaign_candidate.semantic_score = similarity
        campaign_candidate.semantic_score_breakdown = breakdown
        self.campaign_candidate_repository.update(campaign_candidate)

        return breakdown

    def _read_threshold(self) -> float:
        raw = self.config_repository.get_configs_by_keys([SEMANTIC_PASS_THRESHOLD_KEY]).get(
            SEMANTIC_PASS_THRESHOLD_KEY,
        )
        return float(raw) if raw is not None else _DEFAULT_SEMANTIC_PASS_THRESHOLD

    @staticmethod
    def _skills_from_deterministic_breakdown(campaign_candidate) -> tuple[list[str], list[str]]:
        """
        Reuses the mandatory/preferred skill matches the deterministic
        layer already computed and stored on score_breakdown - never
        re-runs skill-ontology matching here.
        """
        breakdown = campaign_candidate.score_breakdown or {}
        entries = (breakdown.get("mandatory_skills") or []) + (breakdown.get("preferred_skills") or [])

        matching: list[str] = []
        missing: list[str] = []
        for entry in entries:
            name = entry.get("canonical_name")
            if not name:
                continue
            if entry.get("match_type") == "MISSING":
                missing.append(name)
            else:
                matching.append(name)
        return matching, missing

    def _matched_keywords(self, jd_id: UUID, resume_id: UUID) -> list[str]:
        """
        Raw-text keyword overlap between the JD's extracted required/
        preferred skill strings and the resume's extracted skill strings -
        a simple signal independent of matching_skills (which is
        ontology-normalized): a plain case-insensitive set intersection, no
        new matching engine, no embedding involved.
        """
        job_description = self.jd_repository.get_by_id(jd_id)
        resume = self.resume_repository.get_by_id(resume_id)

        jd_json = (job_description.extracted_json if job_description else None) or {}
        resume_json = (resume.parsed_json if resume else None) or {}

        jd_terms = {
            str(term).strip().lower()
            for term in (jd_json.get("required_skills") or []) + (jd_json.get("preferred_skills") or [])
            if term
        }
        resume_terms = {str(term).strip().lower() for term in (resume_json.get("skills") or []) if term}

        return sorted(jd_terms & resume_terms)

    @staticmethod
    def _build_explanation(similarity: float, threshold: float, passed: bool) -> str:
        verb = "meets" if passed else "falls short of"
        return (
            f"Resume-to-job semantic similarity is {similarity * 100:.1f}%, which {verb} "
            f"the configured threshold of {threshold * 100:.1f}%."
        )
