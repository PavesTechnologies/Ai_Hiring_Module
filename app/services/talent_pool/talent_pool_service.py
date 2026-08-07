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
from app.schemas.talent_pool.talent_pool_schema import (
    AddCandidateToCampaignResponse,
    CampaignSummaryResponse,
    CandidateInfoResponse,
    ConsentInfoResponse,
    PerformanceSummaryResponse,
    ResumeInfoResponse,
    TalentPoolCandidateProfileResponse,
    TalentPoolInfoResponse,
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
                is_talent_pool_eligible=bool(embedding.is_talent_pool_eligible) if embedding is not None else False,
                embedding_updated_at=embedding.created_at if embedding is not None else None,
            ),
            resume=ResumeInfoResponse(
                active_resume_version=resume.version_number if resume is not None else None,
                uploaded_at=resume.created_at if resume is not None else None,
                parse_status=resume.parse_status if resume is not None else None,
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
