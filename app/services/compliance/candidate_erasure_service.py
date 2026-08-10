import logging
from datetime import datetime, timezone
from uuid import UUID

from app.enums.constants import ActionType, EntityType
from app.exception_handler.exceptions import NotFoundError
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.candidate_repository import CandidateRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.consent_repository import ConsentRepository
from app.repositories.dead_letter_queue_repository import DeadLetterQueueRepository
from app.repositories.email_notification_repository import EmailNotificationRepository
from app.repositories.resume_repository import ResumeRepository
from app.services.audit_service import AuditService
from app.repositories.candidate_composite_score_history_repository import (
    CandidateCompositeScoreHistoryRepository,
)

logger = logging.getLogger(__name__)

# Matches the literal every other resume-touching service already uses
# (ResumeService.RESUME_STORAGE_BUCKET, ResumeUploadService.RESUME_STORAGE_BUCKET,
# CampaignCandidateService._RESUME_STORAGE_BUCKET) — not worth a shared
# constant module just for this one string.
_RESUME_STORAGE_BUCKET = "airs_resumes"


class CandidateErasureService:
    """
    GDPR-style right-to-erasure — permanently removes a candidate and every
    row that references them (resumes, resume files in storage, scores,
    skills, embeddings, pipeline/campaign history, consent, notifications),
    regardless of whether the candidate originated from an individual or a
    bulk ZIP upload. Both origins land in identical tables (only
    Resume.bulk_upload_job_id differs), and bulk_upload_jobs/
    bulk_upload_job_files hold no live reference back to the candidate, so
    no bulk-specific handling is needed here.

    The one deliberate exception: the audit_log entry this method itself
    writes survives, since AuditLog.entity_id is a bare UUID with no FK to
    candidates.id — that's what makes the audit trail requirement and the
    "no trace" requirement compatible.
    """

    def __init__(
        self,
        candidate_repo: CandidateRepository,
        resume_repo: ResumeRepository,
        campaign_candidate_repo: CampaignCandidateRepository,
        consent_repo: ConsentRepository,
        email_notification_repo: EmailNotificationRepository,
        celery_task_log_repo: CeleryTaskLogRepository,
        dead_letter_queue_repo: DeadLetterQueueRepository,
        storage_service,
        audit_service: AuditService,
        composite_score_history_repo: CandidateCompositeScoreHistoryRepository,
    ):
        self.candidate_repo = candidate_repo
        self.resume_repo = resume_repo
        self.campaign_candidate_repo = campaign_candidate_repo
        self.consent_repo = consent_repo
        self.email_notification_repo = email_notification_repo
        self.celery_task_log_repo = celery_task_log_repo
        self.dead_letter_queue_repo = dead_letter_queue_repo
        self.storage_service = storage_service
        self.audit_service = audit_service
        self.composite_score_history_repo = composite_score_history_repo

    def erase_candidate(
        self,
        candidate_id: UUID,
        actor_id: str,
        actor_role: str | None,
        reason: str | None = None,
    ) -> None:
        candidate = self.candidate_repo.get_by_id(candidate_id)
        if candidate is None:
            raise NotFoundError(f"Candidate {candidate_id} not found.")

        try:
            resumes = self.resume_repo.get_all_versions_by_candidate(candidate_id)
            campaign_candidates = self.campaign_candidate_repo.get_by_candidate_id(candidate_id)
            had_bulk_origin = any(resume.bulk_upload_job_id is not None for resume in resumes)

            self.resume_repo.delete_embeddings_by_candidate(candidate_id)
            self.resume_repo.delete_candidate_skills_by_candidate(candidate_id)

            for campaign_candidate in campaign_candidates:
                # candidate_rejections is gone - the AI evaluation row
                # cascades automatically (cascade="all, delete-orphan" on
                # CampaignCandidate.ai_evaluation) when the campaign_candidate
                # itself is deleted below.
                self.campaign_candidate_repo.delete_stage_history(campaign_candidate.id)
                self.composite_score_history_repo.delete_by_campaign_candidate_id(campaign_candidate.id)
                # DLQ before celery_task_log — dead_letter_queue.original_task_id
                # is a NOT NULL FK to celery_task_log.task_id.
                self.dead_letter_queue_repo.delete_by_campaign_candidate_id(campaign_candidate.id)
                self.celery_task_log_repo.delete_by_campaign_candidate_id(campaign_candidate.id)
                self.campaign_candidate_repo.delete(campaign_candidate)

            self.email_notification_repo.delete_by_candidate(candidate_id)

            for resume in resumes:
                self.resume_repo.delete_parse_attempts(resume.id)
                self.dead_letter_queue_repo.delete_by_resume_id(resume.id)
                self.celery_task_log_repo.delete_by_resume_id(resume.id)
                self._delete_resume_file(resume.file_path)
                self.resume_repo.delete(resume)

            self.consent_repo.delete_by_candidate(candidate_id)
            self.candidate_repo.delete(candidate)

            self.audit_service.log(
                actor_id=actor_id,
                actor_role=actor_role,
                action_type=ActionType.CANDIDATE_DATA_ERASED,
                entity_type=EntityType.CANDIDATE,
                entity_id=candidate_id,
                details={
                    "resume_versions_deleted": len(resumes),
                    "campaign_candidates_deleted": len(campaign_candidates),
                    "had_bulk_origin": had_bulk_origin,
                    "reason": reason,
                },
            )

            self.candidate_repo.commit()
        except Exception:
            self.candidate_repo.rollback()
            raise

    def request_erasure(
        self,
        candidate_id: UUID,
        actor_id: str,
        actor_role: str | None,
        reason: str | None = None,
    ) -> None:
        """
        Candidate Erasure - "requested" phase, distinct from erase_candidate
        (the full hard-delete flow above): an immediate, non-destructive
        privacy safeguard for the moment erasure is first requested,
        before the full cascade delete necessarily happens. Unlike
        erase_candidate:
          - resume_embeddings rows are RETAINED (never deleted) for
            referential integrity - each row's embedding vector is
            overwritten with a 384-dimension zero vector instead, via
            ResumeRepository.zero_out_embeddings_for_candidate.
          - is_talent_pool_eligible is set FALSE on every one of those
            rows (same repository call).
          - jd_embeddings is never touched - JDs are never candidate PII.
          - candidate.updated_at and every resume.updated_at are bumped
            (neither has an onupdate=func.now() at the DB level, so this
            must be done explicitly, same convention as every other
            manual updated_at bump in this codebase, e.g.
            deterministic_scoring_tasks.py's campaign_candidate.updated_at).

        Reuses ActionType.CANDIDATE_DATA_ERASED (already live in the DB
        enum - no new migration) for the audit log, with
        details.phase="requested" distinguishing it from erase_candidate's
        own (phase="completed") log entry.
        """
        candidate = self.candidate_repo.get_by_id(candidate_id)
        if candidate is None:
            raise NotFoundError(f"Candidate {candidate_id} not found.")

        try:
            now = datetime.now(timezone.utc)

            self.candidate_repo.update_erasure_fields(candidate_id, erasure_requested_at=now)
            candidate.updated_at = now

            resumes = self.resume_repo.get_all_versions_by_candidate(candidate_id)
            for resume in resumes:
                resume.updated_at = now

            embeddings_zeroed = self.resume_repo.zero_out_embeddings_for_candidate(candidate_id)

            self.audit_service.log(
                actor_id=actor_id,
                actor_role=actor_role,
                action_type=ActionType.CANDIDATE_DATA_ERASED,
                entity_type=EntityType.CANDIDATE,
                entity_id=candidate_id,
                details={
                    "phase": "requested",
                    "resume_embeddings_zeroed": embeddings_zeroed,
                    "resume_versions_touched": len(resumes),
                    "reason": reason,
                },
            )

            self.candidate_repo.commit()
        except Exception:
            self.candidate_repo.rollback()
            raise

    def _delete_resume_file(self, file_path: str) -> None:
        """
        Best-effort, same convention as every other storage-cleanup call site
        in this codebase (JDProcessingPipeline, bulk_upload_tasks) — a
        transient storage-provider error must not block the legally-required
        DB-side erasure, so it's logged, not raised.
        """
        try:
            self.storage_service.delete_file(_RESUME_STORAGE_BUCKET, file_path)
        except Exception:
            logger.exception("Failed to delete resume file '%s' from storage during candidate erasure.", file_path)
