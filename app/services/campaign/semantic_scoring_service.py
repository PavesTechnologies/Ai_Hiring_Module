import logging
import time
from datetime import datetime, timezone
from uuid import UUID

from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.jd_repository import JDRepository
from app.repositories.resume_repository import ResumeRepository

logger = logging.getLogger(__name__)

# Story 540: literal marker recorded on celery_task_log.output_summary
# (via the breakdown dict, which the caller writes verbatim into that
# column) whenever a raw similarity < 0.0 is clamped up to 0.0000.
SCORE_CLAMPED_TO_ZERO_REASON = "SCORE_CLAMPED_TO_ZERO"


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
        campaign_candidate_repository: CampaignCandidateRepository,
    ):
        self.resume_repository = resume_repository
        self.jd_repository = jd_repository
        self.campaign_candidate_repository = campaign_candidate_repository

    def calculate_and_store_semantic_score_breakdown(
        self,
        campaign_candidate_id: UUID,
        jd_id: UUID,
        resume_id: UUID,
        semantic_threshold: float,
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

        # Story 538: the entire formula - 1 - (re.embedding <=> je.embedding)
        # - runs as one statement in Postgres via
        # ResumeRepository.compute_semantic_similarity; embedding_model_version_id
        # equality is enforced as part of that same query, and neither raw
        # vector is ever fetched into application memory here.
        computation_started_at = time.perf_counter()
        similarity = self.resume_repository.compute_semantic_similarity(resume_id, jd_id)
        computation_duration_ms = round((time.perf_counter() - computation_started_at) * 1000)
        if similarity is None:
            raise MissingResumeEmbeddingError(
                f"Resume '{resume_id}' embedding could not be compared against JD '{jd_id}' embedding - "
                "either row may have been deleted concurrently, or their embedding_model_version_id "
                "no longer match."
            )

        # Story 540: any score >= 0.0 is valid as-is, including very low
        # ones - only a genuinely negative value (real negative cosine
        # similarity, or floating-point rounding right at the boundary) is
        # clamped, and only ever up to 0.0000, never down.
        score_clamped_to_zero = False
        if similarity < 0.0:
            logger.warning(
                "%s | campaign_candidate_id=%s raw_similarity=%s",
                SCORE_CLAMPED_TO_ZERO_REASON, campaign_candidate_id, similarity,
            )
            similarity = 0.0
            score_clamped_to_zero = True

        # Story 541: per-campaign threshold (hiring_campaigns.semantic_threshold),
        # never a global platform_config value - mirrors how
        # calculate_deterministic_score_task already reads
        # campaign.deterministic_threshold for the same purpose.
        threshold = float(semantic_threshold)
        # Story 540: threshold comparison uses the stored (clamped) score.
        passed = similarity >= threshold

        matching_skills, missing_skills = self._skills_from_deterministic_breakdown(campaign_candidate)
        matched_keywords = self._matched_keywords(jd_id, resume_id)

        computed_at = datetime.now(timezone.utc)
        breakdown = {
            "semantic_score": round(similarity, 4),
            "overall_similarity": round(similarity, 4),
            "semantic_passed": passed,
            "semantic_threshold": threshold,
            "matching_skills": matching_skills,
            "missing_skills": missing_skills,
            "matched_keywords": matched_keywords,
            "semantic_explanation": self._build_explanation(similarity, threshold, passed),
            "resume_embedding_model_version_id": str(resume_embedding.embedding_model_version_id),
            "jd_embedding_model_version_id": str(jd_embedding.embedding_model_version_id),
            "computed_at": computed_at.isoformat(),
            "computation_duration_ms": computation_duration_ms,
            "score_clamped_to_zero": score_clamped_to_zero,
            "score_clamp_reason": SCORE_CLAMPED_TO_ZERO_REASON if score_clamped_to_zero else None,
            # Task 539: jd_embedding_id recorded on the semantic breakdown
            # for traceability - which exact jd_embeddings row this score
            # was computed against.
            "semantic_check": {
                "jd_embedding_id": str(jd_embedding.id),
                "resume_embedding_id": str(resume_embedding.id),
            },
        }

        # Task 539: semantic_score, semantic_score_computed_at and
        # updated_at all change together on this one ORM object before a
        # single update()/commit() - one row, one transaction, so this is
        # already atomic without any extra locking.
        campaign_candidate.semantic_score = similarity
        campaign_candidate.semantic_passed = passed
        campaign_candidate.semantic_breakdown = breakdown
        campaign_candidate.semantic_score_computed_at = computed_at
        campaign_candidate.updated_at = computed_at
        self.campaign_candidate_repository.update(campaign_candidate)

        return breakdown

    @staticmethod
    def _skills_from_deterministic_breakdown(campaign_candidate) -> tuple[list[str], list[str]]:
        """
        Reuses the mandatory/preferred skill matches the deterministic
        layer already computed and stored on score_breakdown - never
        re-runs skill-ontology matching here.
        """
        breakdown = campaign_candidate.deterministic_breakdown or {}
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
