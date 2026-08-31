import logging
from uuid import UUID

from app.enums.constants import ActionType, EntityType
from app.exception_handler.exceptions import NotFoundError
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.dead_letter_queue_repository import DeadLetterQueueRepository
from app.repositories.resume_repository import ResumeRepository
from app.services.audit_service import AuditService
from app.core.cache_keys import candidate_list_prefix, resume_key, resume_list_prefix
from app.services.cache_service import CacheService

logger = logging.getLogger(__name__)

# Matches the literal every other resume-touching service already uses.
_RESUME_STORAGE_BUCKET = "airs_resumes"


class ResumeCleanupService:
    """
    Deletes a single resume version and everything that references it,
    without touching the candidate itself, the candidate's other resume
    versions, or any other campaign the candidate is linked to through a
    different resume. Built for cleaning up stuck/orphaned resumes (e.g. a
    processing task that was enqueued but never picked up) — a narrower,
    non-destructive alternative to CandidateErasureService, which removes
    the whole candidate.

    A resume can be linked to more than one campaign (the same file reused
    via "use existing" duplicate resolution) — every campaign_candidates
    row pointing at this resume is removed, since none of them can be left
    referencing a resume that no longer exists.
    """

    def __init__(
        self,
        resume_repo: ResumeRepository,
        campaign_candidate_repo: CampaignCandidateRepository,
        celery_task_log_repo: CeleryTaskLogRepository,
        dead_letter_queue_repo: DeadLetterQueueRepository,
        storage_service,
        audit_service: AuditService,
        cache_service: CacheService | None = None,
    ):
        self.resume_repo = resume_repo
        self.campaign_candidate_repo = campaign_candidate_repo
        self.celery_task_log_repo = celery_task_log_repo
        self.dead_letter_queue_repo = dead_letter_queue_repo
        self.storage_service = storage_service
        self.audit_service = audit_service
        self.cache_service = cache_service

    def delete_resume(
        self,
        resume_id: UUID,
        actor_id: str,
        actor_role: str | None,
        reason: str | None = None,
    ) -> None:
        resume = self.resume_repo.get_by_id(resume_id)
        if resume is None:
            raise NotFoundError(f"Resume {resume_id} not found.")

        try:
            campaign_candidates = self.campaign_candidate_repo.get_by_resume_id(resume_id)

            for campaign_candidate in campaign_candidates:
                # candidate_rejections is gone - the AI evaluation row
                # cascades automatically (cascade="all, delete-orphan" on
                # CampaignCandidate.ai_evaluation) when the campaign_candidate
                # itself is deleted below.
                self.campaign_candidate_repo.delete_stage_history(campaign_candidate.id)
                # DLQ before celery_task_log — dead_letter_queue.original_task_id
                # is a NOT NULL FK to celery_task_log.task_id.
                self.dead_letter_queue_repo.delete_by_campaign_candidate_id(campaign_candidate.id)
                self.celery_task_log_repo.delete_by_campaign_candidate_id(campaign_candidate.id)
                self.campaign_candidate_repo.delete(campaign_candidate)

            self.resume_repo.delete_parse_attempts(resume_id)
            self.resume_repo.delete_embedding_by_resume(resume_id)
            self.resume_repo.delete_candidate_skills_by_resume(resume_id)
            self.dead_letter_queue_repo.delete_by_resume_id(resume_id)
            self.celery_task_log_repo.delete_by_resume_id(resume_id)

            self._delete_resume_file(resume.file_path)

            candidate_id = resume.candidate_id
            was_active_version = resume.is_active_version
            bulk_upload_job_id = resume.bulk_upload_job_id
            campaign_ids_affected = [str(cc.campaign_id) for cc in campaign_candidates]

            self.resume_repo.delete(resume)

            self.audit_service.log(
                actor_id=actor_id,
                actor_role=actor_role,
                action_type=ActionType.RESUME_DELETED,
                entity_type=EntityType.RESUME,
                entity_id=resume_id,
                details={
                    "candidate_id": str(candidate_id),
                    "was_active_version": was_active_version,
                    "had_bulk_origin": bulk_upload_job_id is not None,
                    "campaign_candidates_removed": len(campaign_candidates),
                    "campaign_ids_affected": campaign_ids_affected,
                    "reason": reason,
                },
            )

            self.resume_repo.commit()
        except Exception:
            self.resume_repo.rollback()
            raise

        if self.cache_service:
            self.cache_service.delete(resume_key(resume_id))
            self.cache_service.delete_by_prefix(resume_list_prefix())
            self.cache_service.delete_by_prefix(candidate_list_prefix())

    def _delete_resume_file(self, file_path: str) -> None:
        """Best-effort — same convention as CandidateErasureService._delete_resume_file."""
        try:
            self.storage_service.delete_file(_RESUME_STORAGE_BUCKET, file_path)
        except Exception:
            logger.exception("Failed to delete resume file '%s' from storage during resume cleanup.", file_path)
