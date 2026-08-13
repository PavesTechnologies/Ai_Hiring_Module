import hashlib
import logging
import time
from uuid import UUID, uuid4

from app.core.encryption_service import DecryptionError, EncryptionService
from app.enums.constants import ActionType, EntityType
from app.enums.education import DegreeLevel, EducationField
from app.exception_handler.exceptions import BadRequestError, NotFoundError, UnprocessableError
from app.exceptions.campaign_exceptions import CampaignException
from app.models.async_tasks import CeleryTaskLog, TaskStatus
from app.models.campaigns import CampaignStatus, HiringCampaign
from app.models.candidates import Candidate, Resume
from app.models.pipeline import CampaignCandidate, CampaignCandidateStageHistory, PipelineStage, TransitionSource
from sqlalchemy.exc import IntegrityError
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.candidate_repository import CandidateRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.consent_repository import ConsentRepository
from app.repositories.resume_repository import ResumeRepository
from app.repositories.skill_repository import SkillRepository
from app.core.config import settings
from app.core.cache_keys import reference_key
from app.services.cache_service import CacheService
from app.schemas.talent_pool.talent_pool_schema import (
    AddCandidateToCampaignResponse,
    BulkAddCandidateResultItem,
    BulkAddCandidatesResponse,
    CampaignFilterOption,
    CampaignSummaryResponse,
    CandidateInfoResponse,
    ConsentInfoResponse,
    EducationFilterOptions,
    PerformanceSummaryResponse,
    ResumeInfoResponse,
    TalentPoolCandidateProfileResponse,
    TalentPoolFiltersResponse,
    TalentPoolInfoResponse,
    TalentPoolSearchItem,
    TalentPoolSearchResponse,
    TalentPoolSemanticSearchFilters,
    TalentPoolSemanticSearchItem,
    TalentPoolSemanticSearchResponse,
)
from app.services.ai.embedding_service import EmbeddingService
from app.services.audit_service import AuditService
from app.services.campaign.resume_selection_service import ResumeSelectionResult, ResumeSelectionService
from app.services.celery_task_log_service import CeleryTaskLogService
from app.services.skills.skill_normalization_service import load_cached_active_skills
from app.services.resume.resume_service import ResumeService
from app.tasks.embedding_tasks import EMBED_RESUME_TASK_TYPE, _enqueue_resume_embedding
from app.tasks.resume_processing_tasks import RESUME_DOCUMENT_PROCESSING_TASK_TYPE, process_resume_document

logger = logging.getLogger(__name__)

# M13-E01 S01 T03: no dedicated Celery task exists for skill normalization
# anywhere in this codebase — it runs inline inside the
# RESUME_DOCUMENT_PROCESSING pipeline (see resume_processing_tasks.py), not
# as its own dispatchable task. Mirrors the exact same forward-compatible
# placeholder convention CampaignCandidateService already established for
# AI_EVALUATE_TASK_TYPE/SEMANTIC_SCORE_TASK_TYPE: "queuing" is recorded as a
# QUEUED celery_task_log row only, with no real apply_async call.
SKILL_NORMALIZE_TASK_TYPE = "SKILL_NORMALIZE"

_ALREADY_IN_CAMPAIGN_MESSAGE = "Candidate already exists in this campaign."
_GENERIC_BULK_FAILURE_REASON = "An unexpected error occurred while adding this candidate."

# Talent Pool Normal Search - maximum candidates returned per page,
# enforced server-side regardless of what a caller requests.
TALENT_POOL_MAX_PAGE_SIZE = 6

# Mirrors ResumeSelectionService's own private config key/default exactly
# (see resume_selection_service.py's _RESUME_FRESHNESS_MAX_AGE_DAYS_KEY /
# _DEFAULT_RESUME_FRESHNESS_MAX_AGE_DAYS) - duplicated as a literal only so
# the same freshness rule can be pushed into a SQL WHERE clause here,
# without modifying or re-implementing ResumeSelectionService.
_RESUME_FRESHNESS_MAX_AGE_DAYS_KEY = "RESUME_FRESHNESS_MAX_AGE_DAYS"
_DEFAULT_RESUME_FRESHNESS_MAX_AGE_DAYS = 180


class TalentPoolService:
    """
    M13-E01 S01 — Talent Pool candidate profile (unified view across every
    campaign a candidate has ever been submitted to) and direct add-to-
    campaign. Read-only aggregation reuses CampaignCandidateRepository's
    existing cross-campaign join as-is; nothing here recalculates a score.
    """

    def __init__(
        self,
        candidate_repo: CandidateRepository,
        resume_repo: ResumeRepository,
        campaign_repo: CampaignRepository,
        campaign_candidate_repo: CampaignCandidateRepository,
        consent_repo: ConsentRepository,
        encryption_service: EncryptionService,
        audit_service: AuditService,
        celery_task_log_service: CeleryTaskLogService,
        resume_selection_service: ResumeSelectionService,
        skill_repo: SkillRepository | None = None,
        config_repo: ConfigRepository | None = None,
        embedding_service: EmbeddingService | None = None,
        cache_service: CacheService | None = None,
    ):
        self.cache_service = cache_service
        self.candidate_repo = candidate_repo
        self.resume_repo = resume_repo
        self.campaign_repo = campaign_repo
        self.campaign_candidate_repo = campaign_candidate_repo
        self.consent_repo = consent_repo
        self.encryption_service = encryption_service
        self.audit_service = audit_service
        self.celery_task_log_service = celery_task_log_service
        self.resume_selection_service = resume_selection_service
        self.skill_repo = skill_repo
        self.config_repo = config_repo
        self.embedding_service = embedding_service

    # ------------------------------------------------------------------
    # T01 + T02 — Unified Candidate Profile / Performance Summary
    # ------------------------------------------------------------------

    def get_candidate_profile(self, candidate_id: UUID) -> TalentPoolCandidateProfileResponse:
        candidate = self.candidate_repo.get_by_id(candidate_id)
        if candidate is None:
            raise NotFoundError(f"Candidate {candidate_id} not found.")

        resume = self.resume_repo.get_active_by_candidate(candidate_id)
        embedding = self.resume_repo.get_embedding(resume.id) if resume is not None else None
        latest_consent = self.consent_repo.get_latest_by_candidate(candidate_id)

        campaign_summary, performance_summary = self._build_campaign_and_performance_summary(candidate_id)

        return TalentPoolCandidateProfileResponse(
            candidate=self._build_candidate_info(candidate, resume),
            consent=ConsentInfoResponse(
                consent_given=candidate.consent_given,
                consent_timestamp=candidate.consent_timestamp,
                consent_version=latest_consent.consent_version if latest_consent is not None else None,
            ),
            talent_pool=TalentPoolInfoResponse(
                # Effective eligibility, not the raw stored flag: reuses
                # ResumeSelectionService._is_eligible (PARSED + embedding
                # exists + is_talent_pool_eligible + freshness via
                # RESUME_FRESHNESS_MAX_AGE_DAYS) so this display can never
                # drift from what actually determines campaign selection.
                # The stored resume_embeddings.is_talent_pool_eligible value
                # itself is never modified here.
                is_talent_pool_eligible=(
                    self.resume_selection_service._is_eligible(resume) if resume is not None else False
                ),
                embedding_updated_at=embedding.created_at if embedding is not None else None,
            ),
            resume=ResumeInfoResponse(
                resume_id=resume.id if resume is not None else None,
                active_resume_version=resume.version_number if resume is not None else None,
                uploaded_at=resume.created_at if resume is not None else None,
                parse_status=resume.parse_status if resume is not None else None,
                summary=self._extract_summary(resume),
            ),
            campaign_summary=campaign_summary,
            performance_summary=performance_summary,
        )

    def _build_candidate_info(self, candidate: Candidate, resume: Resume | None) -> CandidateInfoResponse:
        designation, experience, location = self._extract_resume_fields(resume)
        return CandidateInfoResponse(
            candidate_id=candidate.id,
            full_name=self._decrypt_full_name(candidate),
            email=self._decrypt_masked_email(candidate),
            designation=designation,
            experience=experience,
            location=location,
            jurisdiction=candidate.jurisdiction,
        )

    def _build_campaign_and_performance_summary(
        self, candidate_id: UUID,
    ) -> tuple[CampaignSummaryResponse, PerformanceSummaryResponse]:
        # Reused exactly as CampaignCandidateService.get_candidate_campaign_history
        # already consumes it: (campaign_candidate, campaign_name, jd_title)
        # tuples, most recent submission first.
        rows = self.campaign_candidate_repo.get_all_by_candidate_across_campaigns(candidate_id)
        total_campaigns = len(rows)
        top_5_skills = self.resume_repo.get_top_skills_by_candidate(candidate_id, limit=5)

        if not rows:
            return (
                CampaignSummaryResponse(total_campaigns=0, latest_campaign=None, latest_pipeline_stage=None),
                PerformanceSummaryResponse(
                    best_composite_score=None,
                    campaign_name=None,
                    jd_title=None,
                    average_composite_score=None,
                    shortlisted_count=0,
                    selected_count=0,
                    total_campaigns=0,
                    top_5_skills=top_5_skills,
                ),
            )

        latest_campaign_candidate, latest_campaign_name, _ = rows[0]
        campaign_summary = CampaignSummaryResponse(
            total_campaigns=total_campaigns,
            latest_campaign=latest_campaign_name,
            latest_pipeline_stage=latest_campaign_candidate.pipeline_stage,
        )

        scored_rows = [row for row in rows if row[0].composite_score is not None]
        best_row = max(scored_rows, key=lambda row: row[0].composite_score, default=None)
        average_composite_score = (
            round(sum(float(row[0].composite_score) for row in scored_rows) / len(scored_rows), 2)
            if scored_rows else None
        )
        shortlisted_count = sum(1 for row in rows if row[0].pipeline_stage == PipelineStage.SHORTLISTED)
        selected_count = sum(1 for row in rows if row[0].pipeline_stage == PipelineStage.SELECTED)

        performance_summary = PerformanceSummaryResponse(
            best_composite_score=float(best_row[0].composite_score) if best_row is not None else None,
            campaign_name=best_row[1] if best_row is not None else None,
            jd_title=best_row[2] if best_row is not None else None,
            average_composite_score=average_composite_score,
            shortlisted_count=shortlisted_count,
            selected_count=selected_count,
            total_campaigns=total_campaigns,
            top_5_skills=top_5_skills,
        )

        return campaign_summary, performance_summary

    @staticmethod
    def _extract_resume_fields(resume: Resume | None) -> tuple[str | None, float | None, str | None]:
        """
        Reads designation/experience/location straight out of the
        already-parsed resume JSON (ResumeExtractionResponse's shape) —
        never re-parses or re-extracts anything. Mirrors
        CampaignCandidateService._extract_designation_and_experience's
        designation/experience convention, plus location.
        """
        if resume is None or not resume.parsed_json:
            return None, None, None

        parsed = resume.parsed_json
        experience = parsed.get("total_experience_years")
        location = parsed.get("location")

        work_experience = parsed.get("work_experience") or []
        designation = None
        current_entry = next((entry for entry in work_experience if entry.get("is_current")), None)
        entry = current_entry or (work_experience[0] if work_experience else None)
        if entry:
            designation = entry.get("title")

        return designation, experience, location

    def _decrypt_full_name(self, candidate: Candidate) -> str | None:
        if not candidate.full_name_encrypted:
            return None
        try:
            return self.encryption_service.decrypt(candidate.full_name_encrypted, candidate.encryption_key_id)
        except DecryptionError:
            logger.exception("Failed to decrypt candidate name for candidate_id=%s", candidate.id)
            return None

    def _decrypt_masked_email(self, candidate: Candidate) -> str | None:
        if not candidate.email_encrypted:
            return None
        try:
            email = self.encryption_service.decrypt(candidate.email_encrypted, candidate.encryption_key_id)
        except DecryptionError:
            logger.exception("Failed to decrypt candidate email for candidate_id=%s", candidate.id)
            return None
        return self._mask_email(email)

    @staticmethod
    def _mask_email(email: str) -> str:
        """Keeps the first character of the local part and the full domain — e.g. j***n@example.com."""
        local, _, domain = email.partition("@")
        if not domain or not local:
            return "***"
        visible = local[0]
        return f"{visible}{'*' * max(len(local) - 1, 3)}@{domain}"

    # ------------------------------------------------------------------
    # S02 — Talent Pool Search and Skill-Based Filtering
    # ------------------------------------------------------------------

    def search_candidates(
        self,
        *,
        search: str | None = None,
        skill: str | None = None,
        skills: list[str] | None = None,
        designation: str | None = None,
        designations: list[str] | None = None,
        location: str | None = None,
        locations: list[str] | None = None,
        degree_levels: list[str] | None = None,
        education_fields: list[str] | None = None,
        campaign_ids: list[UUID] | None = None,
        pipeline_stages: list[PipelineStage] | None = None,
        experience_min: float | None = None,
        experience_max: float | None = None,
        score_min: float | None = None,
        score_max: float | None = None,
        campaign_id: UUID | None = None,
        page: int = 1,
        size: int = 20,
    ) -> TalentPoolSearchResponse:
        """
        Read-only search/filter over the Talent Pool — every filter is
        applied as a SQL WHERE condition by
        ResumeRepository.search_talent_pool (COUNT and the LIMIT/OFFSET page
        are the exact same filtered query), never by loading the Talent Pool
        into Python and filtering here. Never selects or persists a resume —
        that stays exclusively add_candidate_to_campaign's job via
        ResumeSelectionService. Eligibility (PARSED + has an
        is_talent_pool_eligible embedding + fresher than the
        RESUME_FRESHNESS_MAX_AGE_DAYS platform config) mirrors
        ResumeSelectionService._is_eligible's own predicate, expressed as
        SQL by the repository rather than re-implemented here — only the
        platform-config lookup itself is duplicated (a single small read),
        never ResumeSelectionService's logic.

        `search` is the Normal Search box: a candidate matches if EITHER
        their name contains the whole search string, OR every
        whitespace-separated token matches a distinct skill (AND) - e.g.
        "Python AWS" requires both skills, "Ajay" matches on name. `skills`
        (repeatable) and singular `skill` (kept for backward compatibility)
        remain an independent OR'd-together skill filter. `designation`/
        `designations` and `location`/`locations` fold into one OR'd,
        case-insensitive substring term list each (multi-select checkbox
        filters). `degree_levels`/`education_fields`/`campaign_ids`/
        `pipeline_stages` are each OR'd within their own category; every
        distinct filter category combines with AND. `campaign_id` (singular,
        exclusion) is the pre-existing "who's left to add for this specific
        campaign" filter and stays independent of the new inclusion-based
        `campaign_ids`. `score_min`/`score_max` filter on the same
        best_composite_score already shown on the card (MAX across every
        campaign the candidate has ever been submitted to) - never a
        semantic/AI score.

        Page size is capped at TALENT_POOL_MAX_PAGE_SIZE regardless of what
        the caller requests.
        """
        capped_size = max(1, min(size, TALENT_POOL_MAX_PAGE_SIZE))

        or_skill_terms = list(dict.fromkeys([*(skills or []), *([skill] if skill else [])]))
        designation_terms = list(dict.fromkeys([*(designations or []), *([designation] if designation else [])]))
        location_terms = list(dict.fromkeys([*(locations or []), *([location] if location else [])]))

        search_tokens = search.split() if search else []
        all_skill_terms = list(dict.fromkeys([*or_skill_terms, *search_tokens]))
        resolved_skill_ids_by_term = self._resolve_skill_terms(all_skill_terms)

        page_resumes, total = self.resume_repo.search_talent_pool(
            search=search,
            or_skill_terms=or_skill_terms or None,
            designation_terms=designation_terms or None,
            location_terms=location_terms or None,
            degree_levels=degree_levels or None,
            education_fields=education_fields or None,
            campaign_ids=campaign_ids or None,
            exclude_campaign_id=campaign_id,
            pipeline_stages=pipeline_stages or None,
            experience_min=experience_min,
            experience_max=experience_max,
            score_min=score_min,
            score_max=score_max,
            resolved_skill_ids_by_term=resolved_skill_ids_by_term,
            freshness_max_age_days=self._resume_freshness_max_age_days(),
            page=page,
            size=capped_size,
        )

        candidates_by_id = {
            candidate.id: candidate
            for candidate in self.candidate_repo.get_by_ids([resume.candidate_id for resume in page_resumes])
        }
        page_candidates_and_resumes = [
            (candidates_by_id[resume.candidate_id], resume)
            for resume in page_resumes
            if resume.candidate_id in candidates_by_id
        ]

        # Card enrichment - batched over just this page's candidates/resumes,
        # never one query per candidate.
        page_resume_ids = [resume.id for _, resume in page_candidates_and_resumes]
        page_candidate_ids = [candidate.id for candidate, _ in page_candidates_and_resumes]
        skills_by_resume_id = self.resume_repo.get_canonical_skills_by_resume_ids(page_resume_ids)
        best_composite_by_candidate_id = self.campaign_candidate_repo.get_best_composite_scores_by_candidate_ids(
            page_candidate_ids,
        )

        items = [
            TalentPoolSearchItem(
                candidate=self._build_candidate_info(candidate, resume),
                matching_resume_id=resume.id,
                matching_resume_version=resume.version_number,
                summary=self._extract_summary(resume),
                skills=skills_by_resume_id.get(resume.id, []),
                best_composite_score=best_composite_by_candidate_id.get(candidate.id),
            )
            for candidate, resume in page_candidates_and_resumes
        ]

        return TalentPoolSearchResponse(items=items, total=total, page=page, size=capped_size)

    def _resume_freshness_max_age_days(self) -> int:
        """
        Reads the same RESUME_FRESHNESS_MAX_AGE_DAYS platform config
        ResumeSelectionService._is_fresh reads, so search_talent_pool's SQL
        eligibility window can never drift from add-to-campaign's own
        freshness rule. Falls back to the same default
        ResumeSelectionService itself falls back to if config_repo wasn't
        wired in (e.g. older callers/tests constructing this service
        without it).
        """
        if self.config_repo is None:
            return _DEFAULT_RESUME_FRESHNESS_MAX_AGE_DAYS
        value = self.config_repo.get_configs_by_keys(
            [_RESUME_FRESHNESS_MAX_AGE_DAYS_KEY],
        ).get(_RESUME_FRESHNESS_MAX_AGE_DAYS_KEY)
        return int(value) if value else _DEFAULT_RESUME_FRESHNESS_MAX_AGE_DAYS

    # ------------------------------------------------------------------
    # M14 — Talent Pool Semantic Search
    # ------------------------------------------------------------------

    def semantic_search_candidates(
        self,
        *,
        query: str,
        filters: TalentPoolSemanticSearchFilters | None = None,
        page: int = 1,
        size: int = 6,
    ) -> TalentPoolSemanticSearchResponse:
        """
        M14 — free-text semantic search over the Talent Pool. Pipeline order
        is fixed and non-negotiable: validate query -> apply structured
        filters (reusing ResumeRepository.search_talent_pool's own shared
        filter-condition builder, via semantic_search_talent_pool) to get
        the eligible/filtered candidate set FIRST -> embed the query exactly
        once -> rank ONLY that filtered set by pgvector cosine similarity ->
        paginate. Never the reverse (rank the whole Talent Pool, then
        filter) - semantic_search_talent_pool's WHERE conditions are applied
        before its ORDER BY, entirely in SQL.

        `query` may be a full resume, a JD, a role description, or any other
        free-text passage - it is embedded as one whole meaning-bearing
        text, never split into skill tokens or matched against Normal
        Search's `search`-box rules. Uses the same active embedding model
        (EmbeddingService, reading embedding_model_versions.is_active) that
        already produces every resume_embeddings row - candidate embeddings
        are read as-is, never regenerated here.

        Page size is capped at TALENT_POOL_MAX_PAGE_SIZE, exactly like
        Normal Search. `total` reflects the structured-filtered, eligible
        candidate count - independent of the current page - never merely
        the number of items returned on this page.
        """
        started_at = time.perf_counter()
        trimmed_query = (query or "").strip()
        if not trimmed_query:
            raise BadRequestError("query must not be empty or whitespace-only.")
        if self.embedding_service is None:
            raise UnprocessableError("Semantic search is not available - no embedding service configured.")

        capped_size = max(1, min(size, TALENT_POOL_MAX_PAGE_SIZE))
        filters = filters or TalentPoolSemanticSearchFilters()
        filter_count = sum(
            1 for value in (
                filters.locations, filters.designations, filters.degree_levels, filters.education_fields,
                filters.campaign_ids, filters.pipeline_stages, filters.experience_min, filters.experience_max,
                filters.score_min, filters.score_max,
            ) if value
        )

        try:
            embedding_started_at = time.perf_counter()
            query_embedding = self.embedding_service.generate_embedding(trimmed_query)
            embedding_duration_ms = round((time.perf_counter() - embedding_started_at) * 1000)
        except Exception as exc:
            logger.exception("Talent Pool Semantic Search - embedding generation failed.")
            raise UnprocessableError("Failed to generate an embedding for the search query.") from exc

        active_model_version = self.resume_repo.get_active_embedding_model_version()

        vector_search_started_at = time.perf_counter()
        page_results, total = self.resume_repo.semantic_search_talent_pool(
            query_embedding=query_embedding,
            embedding_model_version_id=active_model_version.id,
            designation_terms=filters.designations or None,
            location_terms=filters.locations or None,
            degree_levels=filters.degree_levels or None,
            education_fields=filters.education_fields or None,
            campaign_ids=filters.campaign_ids or None,
            pipeline_stages=filters.pipeline_stages or None,
            experience_min=filters.experience_min,
            experience_max=filters.experience_max,
            score_min=filters.score_min,
            score_max=filters.score_max,
            freshness_max_age_days=self._resume_freshness_max_age_days(),
            page=page,
            size=capped_size,
        )
        vector_search_duration_ms = round((time.perf_counter() - vector_search_started_at) * 1000)

        candidates_by_id = {
            candidate.id: candidate
            for candidate in self.candidate_repo.get_by_ids([resume.candidate_id for resume, _ in page_results])
        }
        page_rows = [
            (candidates_by_id[resume.candidate_id], resume, similarity)
            for resume, similarity in page_results
            if resume.candidate_id in candidates_by_id
        ]

        # Card enrichment - batched over just this page's candidates/resumes,
        # never one query per candidate. Mirrors search_candidates exactly.
        page_resume_ids = [resume.id for _, resume, _ in page_rows]
        page_candidate_ids = [candidate.id for candidate, _, _ in page_rows]
        skills_by_resume_id = self.resume_repo.get_canonical_skills_by_resume_ids(page_resume_ids)
        best_composite_by_candidate_id = self.campaign_candidate_repo.get_best_composite_scores_by_candidate_ids(
            page_candidate_ids,
        )

        items = [
            TalentPoolSemanticSearchItem(
                candidate=self._build_candidate_info(candidate, resume),
                matching_resume_id=resume.id,
                matching_resume_version=resume.version_number,
                summary=self._extract_summary(resume),
                skills=skills_by_resume_id.get(resume.id, []),
                best_composite_score=best_composite_by_candidate_id.get(candidate.id),
                semantic_similarity_score=round(similarity, 4),
            )
            for candidate, resume, similarity in page_rows
        ]

        total_duration_ms = round((time.perf_counter() - started_at) * 1000)
        logger.info(
            "Talent Pool Semantic Search - mode=SEMANTIC filter_count=%d candidate_count=%d "
            "embedding_duration_ms=%d vector_search_duration_ms=%d total_duration_ms=%d",
            filter_count, total, embedding_duration_ms, vector_search_duration_ms, total_duration_ms,
        )

        return TalentPoolSemanticSearchResponse(items=items, total=total, page=page, size=capped_size)

    @staticmethod
    def _extract_summary(resume: Resume | None) -> str | None:
        """
        Talent Pool card summary - read straight off the matching resume's
        own parsed_json, exactly like _extract_resume_fields reads
        designation/experience/location from the same dict. Never
        generated, never JD-specific: parsed_json is produced once by the
        resume parsing pipeline from the resume alone, with no JD involved.
        """
        if resume is None or not resume.parsed_json:
            return None
        return resume.parsed_json.get("summary")

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    # ------------------------------------------------------------------
    # Talent Pool Normal Search — filter option metadata
    # ------------------------------------------------------------------

    def get_search_filters(self) -> TalentPoolFiltersResponse:
        if not self.cache_service:
            return self._load_search_filters()
        raw = self.cache_service.get_or_set(
            reference_key("talent_pool_filters"),
            loader=lambda: self._load_search_filters().model_dump_json(),
            ttl=settings.cache_reference_ttl_seconds,
        )
        return TalentPoolFiltersResponse.model_validate_json(raw)

    def _load_search_filters(self) -> TalentPoolFiltersResponse:
        """
        Filter option metadata for the Talent Pool Normal Search UI - never
        a candidate search itself. Every list is derived from
        already-persisted data, never hardcoded: locations/designations are
        case-insensitive-deduped from parsed_json (the same fields
        _extract_resume_fields already reads for search/card display),
        education options reuse the resume-extraction pipeline's own
        degree_level/field_normalized controlled vocabulary as-is (no new
        classification logic), campaigns reuse CampaignRepository's
        existing active-campaigns dropdown query, and pipeline stages are
        read straight off the existing PipelineStage enum.
        """
        locations = self._dedupe_case_insensitive(self.resume_repo.get_distinct_locations())
        designations = self._dedupe_case_insensitive(self.resume_repo.get_distinct_designations())

        # UNKNOWN is the AI-extraction pipeline's "not confidently
        # classified" sentinel (see app/enums/education.py) - not a real,
        # filterable category, so it's excluded the same way a blank/NULL
        # value would be.
        degree_levels = sorted({
            value for value in self.resume_repo.get_distinct_education_degree_levels()
            if value and value != DegreeLevel.UNKNOWN.value
        })
        fields = sorted({
            value for value in self.resume_repo.get_distinct_education_fields()
            if value and value != EducationField.UNKNOWN.value
        })

        campaigns = [
            CampaignFilterOption(id=campaign_id, name=name)
            for campaign_id, name in self.campaign_repo.get_active_campaigns_minimal()
        ]

        return TalentPoolFiltersResponse(
            locations=locations,
            designations=designations,
            education=EducationFilterOptions(degree_levels=degree_levels, fields=fields),
            campaigns=campaigns,
            pipeline_stages=[stage.value for stage in PipelineStage],
        )

    def _resolve_skill_terms(self, terms: list[str]) -> dict[str, UUID | None]:
        """
        Resolves every skill/search-box term to a canonical_skill_id in one
        pass over the Redis-cached active-skill catalog, instead of one
        find_skill_by_name_or_alias call per term - that method loaded the
        entire skill_ontology table on every single call, so this was a
        full-table scan multiplied by the number of distinct terms in the
        request. Canonical-name matches take priority over alias matches,
        same as find_skill_by_name_or_alias's own check order.
        """
        if not terms or self.skill_repo is None:
            return {}

        catalog = load_cached_active_skills(self.skill_repo, self.cache_service)
        id_by_lower_name: dict[str, UUID] = {}
        id_by_lower_alias: dict[str, UUID] = {}
        for skill in catalog:
            id_by_lower_name.setdefault(skill.canonical_name.lower(), skill.id)
            for alias in (skill.aliases or []):
                id_by_lower_alias.setdefault(alias.lower(), skill.id)

        return {
            term: id_by_lower_name.get(term.lower()) or id_by_lower_alias.get(term.lower())
            for term in terms
        }

    @staticmethod
    def _dedupe_case_insensitive(rows: list[tuple[str, int]]) -> list[str]:
        """
        Collapses case-only duplicates (e.g. "Hyderabad" / "hyderabad" /
        "HYDERABAD" -> one option), keeping whichever exact casing occurred
        most often across resumes as the canonical display form (ties
        broken alphabetically for determinism) - never mutates the
        underlying resume data, only how these filter options are
        presented. Returned sorted alphabetically.
        """
        best_by_key: dict[str, tuple[str, int]] = {}
        for value, count in rows:
            cleaned = (value or "").strip()
            if not cleaned:
                continue
            key = cleaned.lower()
            current = best_by_key.get(key)
            if current is None or count > current[1] or (count == current[1] and cleaned < current[0]):
                best_by_key[key] = (cleaned, count)
        return sorted(display for display, _ in best_by_key.values())

    # ------------------------------------------------------------------
    # T03 — Add Candidate Directly to New Campaign
    # ------------------------------------------------------------------

    def add_candidate_to_campaign(
        self,
        candidate_id: UUID,
        campaign_id: UUID,
        actor_id: str,
        actor_role: str | None = None,
    ) -> AddCandidateToCampaignResponse:
        candidate = self.candidate_repo.get_by_id(candidate_id)
        if candidate is None:
            raise NotFoundError(f"Candidate {candidate_id} not found.")

        # Locked for the rest of this transaction — mirrors
        # CampaignCandidateService.create_campaign_candidate's exact
        # concurrency-safety pattern for inserting into campaign_candidates.
        campaign = self.campaign_repo.get_by_id_for_update(campaign_id)
        if campaign is None:
            raise CampaignException("Campaign not found.", 404)

        if campaign.status == CampaignStatus.PAUSED:
            raise CampaignException(
                "This campaign is currently paused — uploads are not accepted.", 409,
            )
        if campaign.status != CampaignStatus.ACTIVE:
            raise CampaignException(
                "This campaign is closed and no longer accepting applications.", 403,
            )

        existing = self.campaign_candidate_repo.get_by_campaign_and_candidate(campaign_id, candidate_id)
        if existing is not None:
            raise CampaignException(_ALREADY_IN_CAMPAIGN_MESSAGE, 409)

        # Campaign-specific resume selection (M13-E01 S01 T03 refinement) -
        # replaces the old single global "active resume" lookup.
        # ResumeSelectionService owns eligibility filtering (PARSED, has an
        # eligible embedding, freshness) and the DIRECT-vs-COMPARED
        # selection itself; it raises UnprocessableError directly when no
        # eligible resume exists.
        selection_result = self.resume_selection_service.select_resume_for_campaign(candidate_id, campaign)
        resume = selection_result.selected_resume

        try:
            campaign_candidate = CampaignCandidate(
                campaign_id=campaign_id,
                candidate_id=candidate_id,
                resume_id=resume.id,
                idempotency_key=self._build_idempotency_key(campaign_id, candidate_id, resume.id),
                pipeline_stage=PipelineStage.UPLOADED,
            )
            campaign_candidate, was_created = self.campaign_candidate_repo.create_idempotent(campaign_candidate)

            if not was_created:
                # A retried request under the same idempotency key — return
                # the existing pipeline entry rather than writing a second
                # stage-history row, a duplicate audit entry, or re-queuing
                # tasks that the winning request already queued.
                self.campaign_candidate_repo.commit()
                return self._to_response(campaign_candidate, queued_task_types=[])

            self.campaign_candidate_repo.create_stage_history(
                campaign_candidate_id=campaign_candidate.id,
                from_stage=None,
                to_stage=PipelineStage.UPLOADED,
                transition_source=TransitionSource.SYSTEM,
            )

            self.audit_service.log(
                actor_id=actor_id,
                actor_role=actor_role,
                action_type=ActionType.CANDIDATE_ADDED,
                entity_type=EntityType.CAMPAIGN_CANDIDATE,
                entity_id=campaign_candidate.id,
                campaign_id=campaign_id,
                details={
                    "candidate_id": str(candidate_id),
                    "resume_id": str(resume.id),
                    "pipeline_stage": campaign_candidate.pipeline_stage.value,
                    "source": "TALENT_POOL",
                    "selection_method": selection_result.selection_method.value,
                    "selected_resume_id": str(resume.id),
                    "eligible_resume_count": len(selection_result.evaluated_resumes),
                },
            )

            self.campaign_candidate_repo.commit()
        except Exception:
            self.campaign_candidate_repo.rollback()
            raise

        queued_task_types = self._queue_evaluation_tasks(campaign, resume, campaign_candidate)

        return self._to_response(campaign_candidate, queued_task_types=queued_task_types)

    # ------------------------------------------------------------------
    # Bulk Add Candidates to Campaign
    # ------------------------------------------------------------------

    def bulk_add_candidates_to_campaign(
        self,
        candidate_ids: list[UUID],
        campaign_id: UUID,
        actor_id: str,
        actor_role: str | None = None,
    ) -> BulkAddCandidatesResponse:
        """
        Talent Pool Search -> select multiple candidates -> Bulk Add API.

        True bulk implementation - one campaign fetch/lock and one batched
        duplicate check for the whole request (not once per candidate),
        one bulk insert of the new campaign_candidates rows and one bulk
        insert of their stage-history rows (not one create_idempotent/
        create_stage_history round trip per candidate), and a single
        commit for the whole batch. Resume selection stays a per-candidate
        step - each candidate has independently-versioned resumes/
        embeddings and, when comparing versions, genuine per-resume
        scoring against the JD - this cannot be collapsed into the
        batched steps around it. Audit logging and Celery task enqueueing
        also stay a bounded per-candidate loop (each is one row/one task
        per candidate by nature, not a count/lookup query), matching
        add_candidate_to_campaign's own per-candidate audit/enqueue shape.

        Falls back to the original per-candidate add_candidate_to_campaign
        path (with its own create_idempotent conflict handling) only if
        the bulk insert itself hits a unique-constraint conflict - i.e. a
        genuine race with a concurrent request between the batched
        duplicate check and this insert, which should be rare.

        One candidate's failure (not-found/no-eligible-resume/anything
        else) never blocks or fails any other candidate's outcome, mirroring
        the previous per-item try/except convention.
        """
        # Duplicate candidate_ids in one request must not be added twice
        # (and would otherwise surface as a confusing self-inflicted
        # "already in campaign" failure on the repeat) - dict.fromkeys
        # dedupes while preserving the order candidates were selected in.
        unique_candidate_ids = list(dict.fromkeys(candidate_ids))

        campaign = self.campaign_repo.get_by_id_for_update(campaign_id)
        if campaign is None:
            raise CampaignException("Campaign not found.", 404)
        if campaign.status == CampaignStatus.PAUSED:
            raise CampaignException(
                "This campaign is currently paused — uploads are not accepted.", 409,
            )
        if campaign.status != CampaignStatus.ACTIVE:
            raise CampaignException(
                "This campaign is closed and no longer accepting applications.", 403,
            )

        existing_ids = self.campaign_candidate_repo.get_existing_candidate_ids(
            campaign_id, unique_candidate_ids,
        )

        results: list[BulkAddCandidateResultItem] = []
        to_insert: list[tuple[UUID, CampaignCandidate, Resume, ResumeSelectionResult]] = []

        for candidate_id in unique_candidate_ids:
            if candidate_id in existing_ids:
                results.append(BulkAddCandidateResultItem(
                    candidate_id=candidate_id, status="FAILED", reason=_ALREADY_IN_CAMPAIGN_MESSAGE,
                ))
                continue

            try:
                candidate = self.candidate_repo.get_by_id(candidate_id)
                if candidate is None:
                    raise NotFoundError(f"Candidate {candidate_id} not found.")

                selection_result = self.resume_selection_service.select_resume_for_campaign(
                    candidate_id, campaign,
                )
                resume = selection_result.selected_resume
            except Exception as exc:
                results.append(BulkAddCandidateResultItem(
                    candidate_id=candidate_id, status="FAILED", reason=self._failure_reason(exc),
                ))
                continue

            campaign_candidate = CampaignCandidate(
                id=uuid4(),
                campaign_id=campaign_id,
                candidate_id=candidate_id,
                resume_id=resume.id,
                idempotency_key=self._build_idempotency_key(campaign_id, candidate_id, resume.id),
                pipeline_stage=PipelineStage.UPLOADED,
            )
            to_insert.append((candidate_id, campaign_candidate, resume, selection_result))

        if to_insert:
            try:
                self._bulk_insert_campaign_candidates(to_insert, campaign, actor_id, actor_role)
            except IntegrityError:
                self.campaign_candidate_repo.rollback()
                fallback_items = to_insert
                to_insert = []
                for candidate_id, _, _, _ in fallback_items:
                    try:
                        response = self.add_candidate_to_campaign(
                            candidate_id, campaign_id, actor_id=actor_id, actor_role=actor_role,
                        )
                    except Exception as exc:
                        self.campaign_candidate_repo.rollback()
                        results.append(BulkAddCandidateResultItem(
                            candidate_id=candidate_id, status="FAILED", reason=self._failure_reason(exc),
                        ))
                        continue
                    results.append(BulkAddCandidateResultItem(
                        candidate_id=candidate_id, status="ADDED",
                        campaign_candidate_id=response.campaign_candidate_id, resume_id=response.resume_id,
                    ))

        for candidate_id, campaign_candidate, resume, _ in to_insert:
            results.append(BulkAddCandidateResultItem(
                candidate_id=candidate_id, status="ADDED",
                campaign_candidate_id=campaign_candidate.id, resume_id=resume.id,
            ))
            # Celery enqueue is inherently one call per campaign_candidate
            # row - bounded by request size, and not a DB query, so
            # looping here doesn't reintroduce the N+1 cost the insert/
            # history/dedup batching above removed.
            self._queue_evaluation_tasks(campaign, resume, campaign_candidate)

        return BulkAddCandidatesResponse(
            campaign_id=campaign_id,
            total=len(results),
            added=sum(1 for item in results if item.status == "ADDED"),
            failed=sum(1 for item in results if item.status == "FAILED"),
            results=results,
        )

    def _bulk_insert_campaign_candidates(
        self,
        to_insert: list[tuple[UUID, CampaignCandidate, Resume, ResumeSelectionResult]],
        campaign: HiringCampaign,
        actor_id: str,
        actor_role: str | None,
    ) -> None:
        """
        One flush for every new campaign_candidates row, one flush for
        every new stage-history row, one audit-log insert per candidate
        (bounded, see bulk_add_candidates_to_campaign's docstring), then a
        single commit for the whole batch. Raises IntegrityError
        untouched on a unique-constraint conflict so the caller can fall
        back to the per-candidate path for this batch.
        """
        try:
            campaign_candidates = [campaign_candidate for _, campaign_candidate, _, _ in to_insert]
            self.campaign_candidate_repo.bulk_create(campaign_candidates)

            histories = [
                CampaignCandidateStageHistory(
                    campaign_candidate_id=campaign_candidate.id,
                    from_stage=None,
                    to_stage=PipelineStage.UPLOADED,
                    transition_source=TransitionSource.SYSTEM,
                )
                for campaign_candidate in campaign_candidates
            ]
            self.campaign_candidate_repo.bulk_create_stage_history(histories)

            for candidate_id, campaign_candidate, resume, selection_result in to_insert:
                self.audit_service.log(
                    actor_id=actor_id,
                    actor_role=actor_role,
                    action_type=ActionType.CANDIDATE_ADDED,
                    entity_type=EntityType.CAMPAIGN_CANDIDATE,
                    entity_id=campaign_candidate.id,
                    campaign_id=campaign.id,
                    details={
                        "candidate_id": str(candidate_id),
                        "resume_id": str(resume.id),
                        "pipeline_stage": campaign_candidate.pipeline_stage.value,
                        "source": "TALENT_POOL",
                        "selection_method": selection_result.selection_method.value,
                        "selected_resume_id": str(resume.id),
                        "eligible_resume_count": len(selection_result.evaluated_resumes),
                    },
                )

            self.campaign_candidate_repo.commit()
        except Exception:
            self.campaign_candidate_repo.rollback()
            raise

    @staticmethod
    def _failure_reason(exc: Exception) -> str:
        """
        Caller-safe failure reason for one bulk-add item - never leaks
        internal exception details. NotFoundError/UnprocessableError
        (HTTPException) and CampaignException are exactly the exceptions
        add_candidate_to_campaign deliberately raises with an
        already-caller-safe message; their text is reused verbatim.
        Anything else is unexpected: logged with its full traceback
        server-side, but only a generic message ever reaches the response.
        """
        if isinstance(exc, (NotFoundError, UnprocessableError, CampaignException)):
            detail = getattr(exc, "detail", None)
            if isinstance(detail, str):
                return detail
            return getattr(exc, "message", None) or str(exc)
        logger.exception("Unexpected error in bulk-add-to-campaign for one candidate - reported as FAILED, batch continues.")
        return _GENERIC_BULK_FAILURE_REASON

    def _queue_evaluation_tasks(
        self, campaign: HiringCampaign, resume: Resume, campaign_candidate: CampaignCandidate,
    ) -> list[str]:
        """
        Re-parses an outdated resume from scratch (the existing
        RESUME_DOCUMENT_PROCESSING pipeline — this codebase has no
        separately-named "RESUME_PARSE" task; that pipeline IS the parse
        step), or — when parsing is already current — only refreshes
        SKILL_NORMALIZE + EMBED_RESUME so the new campaign scores against
        fresh skills/embeddings. Broker failures never fail the request —
        matches ResumeIntakeService.upload_resume's exact resilience
        pattern; celery_task_log stays QUEUED (not a terminal failure) for
        the recovery job to pick up later.
        """
        is_outdated = resume.parser_version != ResumeService.PARSER_VERSION

        if is_outdated:
            self._enqueue_resume_reparse(campaign, resume)
            return [RESUME_DOCUMENT_PROCESSING_TASK_TYPE]

        self.celery_task_log_service.create_log(
            task_id=str(uuid4()),
            task_type=SKILL_NORMALIZE_TASK_TYPE,
            campaign_candidate_id=campaign_candidate.id,
        )

        # _enqueue_resume_embedding already no-ops (idempotency-key guarded)
        # if EMBED_RESUME was already queued/run for this resume, and
        # swallows its own apply_async failures — never raises.
        _enqueue_resume_embedding(self.resume_repo.db, resume.id, self.celery_task_log_service)

        return [SKILL_NORMALIZE_TASK_TYPE, EMBED_RESUME_TASK_TYPE]

    def _enqueue_resume_reparse(self, campaign: HiringCampaign, resume: Resume) -> None:
        task_id = uuid4()
        self.resume_repo.set_task_id(resume, str(task_id))
        self.resume_repo.commit()

        task_log_repo = self.celery_task_log_service.repository
        idempotency_key = f"{RESUME_DOCUMENT_PROCESSING_TASK_TYPE}:{resume.id}"
        task_log, was_created = task_log_repo.create_if_new_idempotency_key(
            CeleryTaskLog(
                task_id=str(task_id),
                task_type=RESUME_DOCUMENT_PROCESSING_TASK_TYPE,
                idempotency_key=idempotency_key,
                resume_id=resume.id,
                status=TaskStatus.QUEUED,
            ),
        )
        task_log_repo.commit()

        if not was_created:
            # Already queued by a concurrent request for this exact resume.
            return

        try:
            process_resume_document.apply_async(
                kwargs={"resume_id": str(resume.id), "prompt_template_id": str(campaign.prompt_template_id)},
                task_id=str(task_id),
            )
        except Exception as exc:
            logger.exception(
                "Queue unavailable - resume_id=%s task_id=%s", resume.id, task_id,
            )
            self.celery_task_log_service.mark_dispatch_failed(task_log, str(exc))

    @staticmethod
    def _build_idempotency_key(campaign_id: UUID, candidate_id: UUID, resume_id: UUID) -> str:
        raw = f"{campaign_id}:{candidate_id}:{resume_id}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _to_response(
        campaign_candidate: CampaignCandidate, queued_task_types: list[str],
    ) -> AddCandidateToCampaignResponse:
        return AddCandidateToCampaignResponse(
            campaign_candidate_id=campaign_candidate.id,
            campaign_id=campaign_candidate.campaign_id,
            candidate_id=campaign_candidate.candidate_id,
            resume_id=campaign_candidate.resume_id,
            pipeline_stage=campaign_candidate.pipeline_stage,
            queued_task_types=queued_task_types,
        )
