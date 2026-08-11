import hashlib
import logging
from uuid import UUID, uuid4

from app.core.encryption_service import DecryptionError, EncryptionService
from app.enums.constants import ActionType, EntityType
from app.exception_handler.exceptions import NotFoundError, UnprocessableError
from app.exceptions.campaign_exceptions import CampaignException
from app.models.async_tasks import CeleryTaskLog, TaskStatus
from app.models.campaigns import CampaignStatus, HiringCampaign
from app.models.candidates import Candidate, Resume
from app.models.pipeline import CampaignCandidate, PipelineStage, TransitionSource
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.candidate_repository import CandidateRepository
from app.repositories.consent_repository import ConsentRepository
from app.repositories.resume_repository import ResumeRepository
from app.repositories.skill_repository import SkillRepository
from app.schemas.talent_pool.talent_pool_schema import (
    AddCandidateToCampaignResponse,
    BulkAddCandidateResultItem,
    BulkAddCandidatesResponse,
    CampaignSummaryResponse,
    CandidateInfoResponse,
    ConsentInfoResponse,
    PerformanceSummaryResponse,
    ResumeInfoResponse,
    TalentPoolCandidateProfileResponse,
    TalentPoolInfoResponse,
    TalentPoolSearchItem,
    TalentPoolSearchResponse,
)
from app.services.audit_service import AuditService
from app.services.campaign.resume_selection_service import ResumeSelectionService
from app.services.celery_task_log_service import CeleryTaskLogService
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
    ):
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
        skill: str | None = None,
        skills: list[str] | None = None,
        designation: str | None = None,
        location: str | None = None,
        locations: list[str] | None = None,
        experience_min: float | None = None,
        experience_max: float | None = None,
        campaign_id: UUID | None = None,
        page: int = 1,
        size: int = 20,
    ) -> TalentPoolSearchResponse:
        """
        Read-only search/filter over the Talent Pool. Never selects or
        persists a resume — that stays exclusively add_candidate_to_campaign's
        job via ResumeSelectionService. Eligibility here reuses
        ResumeSelectionService's own _is_eligible predicate directly (the
        exact PARSED + embedding exists + is_talent_pool_eligible + freshness
        check add_candidate_to_campaign's selection is already built on),
        called on the same ResumeSelectionService instance this class already
        depends on — so the two paths can never drift apart, without
        modifying ResumeSelectionService or re-implementing its logic here.

        `skills` (repeatable) and singular `skill` (kept for backward
        compatibility) are folded into one term list and OR'd together — a
        candidate matching ANY term is included, deduped by resume.id so a
        resume matching more than one term isn't double-counted. `locations`
        (repeatable) and singular `location` are folded into their own term
        list the same way — a candidate matches if their location contains
        ANY listed term (case-insensitive substring, OR'd), for the
        multi-location checkbox filter. designation is a single
        case-insensitive substring filter, and experience_min/experience_max
        a numeric range filter — all applied in Python over
        _extract_resume_fields' own (designation, experience, location)
        extraction, the exact same fields already shown on the card, not a
        new source of truth, applied to the eligible candidate set already
        in memory rather than a new query.

        campaign_id, when given, excludes candidates already added to that
        campaign — the "who's left to add" view when browsing the Talent
        Pool to pick candidates for one specific campaign. Purely a
        candidate_id exclusion filter; it never touches campaign-specific
        resume selection (still exclusively ResumeSelectionService's job,
        run only later when a candidate is actually added).
        """
        terms = list(dict.fromkeys([*(skills or []), *([skill] if skill else [])]))

        if terms:
            candidate_resumes = []
            seen_resume_ids: set[UUID] = set()
            for term in terms:
                resolved_skill = self.skill_repo.find_skill_by_name_or_alias(term)
                pattern = f"%{self._escape_like(term)}%"
                for resume in self.resume_repo.get_by_skill_match(
                    canonical_skill_id=resolved_skill.id if resolved_skill is not None else None,
                    raw_text_pattern=pattern,
                ):
                    if resume.id not in seen_resume_ids:
                        seen_resume_ids.add(resume.id)
                        candidate_resumes.append(resume)
        else:
            candidate_resumes = self.resume_repo.get_all_parsed()

        matching_resume_by_candidate: dict[UUID, Resume] = {}
        for resume in candidate_resumes:
            if resume.candidate_id in matching_resume_by_candidate:
                continue
            if self.resume_selection_service._is_eligible(resume):
                matching_resume_by_candidate[resume.candidate_id] = resume

        if campaign_id is not None:
            already_in_campaign = self.campaign_candidate_repo.get_candidate_ids_by_campaign(campaign_id)
            matching_resume_by_candidate = {
                candidate_id: resume
                for candidate_id, resume in matching_resume_by_candidate.items()
                if candidate_id not in already_in_campaign
            }

        if designation:
            designation_lower = designation.lower()
            matching_resume_by_candidate = {
                candidate_id: resume
                for candidate_id, resume in matching_resume_by_candidate.items()
                if designation_lower in (self._extract_resume_fields(resume)[0] or "").lower()
            }

        location_terms = list(dict.fromkeys([*(locations or []), *([location] if location else [])]))
        if location_terms:
            location_terms_lower = [t.lower() for t in location_terms]
            matching_resume_by_candidate = {
                candidate_id: resume
                for candidate_id, resume in matching_resume_by_candidate.items()
                if any(
                    term in (self._extract_resume_fields(resume)[2] or "").lower()
                    for term in location_terms_lower
                )
            }

        if experience_min is not None or experience_max is not None:
            def _experience_in_range(resume: Resume) -> bool:
                experience = self._extract_resume_fields(resume)[1]
                if experience is None:
                    return False
                if experience_min is not None and experience < experience_min:
                    return False
                if experience_max is not None and experience > experience_max:
                    return False
                return True

            matching_resume_by_candidate = {
                candidate_id: resume
                for candidate_id, resume in matching_resume_by_candidate.items()
                if _experience_in_range(resume)
            }

        total = len(matching_resume_by_candidate)
        candidates = self.candidate_repo.get_by_ids(list(matching_resume_by_candidate.keys()))
        candidates.sort(key=lambda candidate: candidate.created_at, reverse=True)

        start = (page - 1) * size
        page_candidates = candidates[start:start + size]

        # Card enrichment - batched over just this page's candidates/resumes,
        # never one query per candidate. matching_resume_by_candidate already
        # holds each candidate's own Resume row (with parsed_json loaded), so
        # the summary needs no extra query at all.
        page_resume_ids = [matching_resume_by_candidate[candidate.id].id for candidate in page_candidates]
        page_candidate_ids = [candidate.id for candidate in page_candidates]
        skills_by_resume_id = self.resume_repo.get_canonical_skills_by_resume_ids(page_resume_ids)
        best_composite_by_candidate_id = self.campaign_candidate_repo.get_best_composite_scores_by_candidate_ids(
            page_candidate_ids,
        )

        items = [
            TalentPoolSearchItem(
                candidate=self._build_candidate_info(candidate, matching_resume_by_candidate[candidate.id]),
                matching_resume_id=matching_resume_by_candidate[candidate.id].id,
                matching_resume_version=matching_resume_by_candidate[candidate.id].version_number,
                summary=self._extract_summary(matching_resume_by_candidate[candidate.id]),
                skills=skills_by_resume_id.get(matching_resume_by_candidate[candidate.id].id, []),
                best_composite_score=best_composite_by_candidate_id.get(candidate.id),
            )
            for candidate in page_candidates
        ]

        return TalentPoolSearchResponse(items=items, total=total, page=page, size=size)

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
        For each candidate, this reuses add_candidate_to_campaign UNCHANGED
        - the exact same campaign-validation/eligibility/duplicate checks,
        the exact same ResumeSelectionService-backed selection (each
        candidate is evaluated independently and may therefore select a
        different resume version), the exact same campaign_candidates
        insert and CANDIDATE_ADDED audit entry, the exact same idempotency
        and Celery evaluation-task dispatch - looped, with an independent
        outcome per candidate. Mirrors SkillCurationService.
        bulk_approve_unknown_skills' established per-item try/commit/
        rollback convention: one candidate's failure - expected
        (not-found/inactive-campaign/already-in-campaign/no-eligible-
        resume) or not - never blocks, fails, or rolls back any other
        candidate's independently committed add.

        No separate/duplicate campaign or eligibility validation happens
        here: add_candidate_to_campaign remains the single source of truth
        for both, called once per candidate exactly as the single-add
        endpoint calls it.
        """
        # Duplicate candidate_ids in one request must not be added twice
        # (and would otherwise surface as a confusing self-inflicted
        # "already in campaign" failure on the repeat) - dict.fromkeys
        # dedupes while preserving the order candidates were selected in.
        unique_candidate_ids = list(dict.fromkeys(candidate_ids))

        results = []
        for candidate_id in unique_candidate_ids:
            try:
                response = self.add_candidate_to_campaign(
                    candidate_id, campaign_id, actor_id=actor_id, actor_role=actor_role,
                )
            except Exception as exc:
                # add_candidate_to_campaign does not roll back on every
                # early-exit path (e.g. the FOR UPDATE lock it acquires on
                # the campaign row before its already-in-campaign/no-
                # eligible-resume checks) - rolling back unconditionally
                # here keeps one failed candidate's lock/transaction state
                # from bleeding into the next iteration on this shared,
                # per-request session. Caught broadly (not just the
                # expected exception types) so a genuinely unexpected error
                # for one candidate still can't take down the whole batch.
                self.campaign_candidate_repo.rollback()
                results.append(BulkAddCandidateResultItem(
                    candidate_id=candidate_id, status="FAILED", reason=self._failure_reason(exc),
                ))
                continue

            results.append(BulkAddCandidateResultItem(
                candidate_id=candidate_id,
                status="ADDED",
                campaign_candidate_id=response.campaign_candidate_id,
                resume_id=response.resume_id,
            ))

        return BulkAddCandidatesResponse(
            campaign_id=campaign_id,
            total=len(results),
            added=sum(1 for item in results if item.status == "ADDED"),
            failed=sum(1 for item in results if item.status == "FAILED"),
            results=results,
        )

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
