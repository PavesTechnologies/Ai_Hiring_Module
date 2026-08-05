import logging
from uuid import UUID, uuid4

from app.enums.constants import ActionType, EntityType
from app.exceptions.campaign_exceptions import CampaignException
from app.models.async_tasks import CeleryTaskLog, TaskStatus
from app.models.campaigns import CampaignStatus, HiringCampaign
from app.models.candidates import Resume
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.schemas.campaign.campaign_candidate_schema import (
    CampaignCandidateCreateRequest,
    CampaignCandidateResponse,
)
from app.services.audit_service import AuditService
from app.services.campaign.campaign_candidate_service import CampaignCandidateService
from app.services.celery_task_log_service import CeleryTaskLogService
from app.services.resume.resume_upload_service import ResumeUploadService
from app.tasks.resume_processing_tasks import (
    RESUME_DOCUMENT_PROCESSING_TASK_TYPE,
    process_resume_document,
)

logger = logging.getLogger(__name__)

UPLOAD_CONSENT_SOURCE = "UPLOAD_FORM"


class ResumeIntakeService:
    """
    Orchestrates the individual-resume-upload endpoint: a cheap campaign
    pre-check (fail fast before storing anything), the actual upload
    (Phase 5), the authoritative, race-safe campaign-candidate insert
    (Phase 6's hardened CampaignCandidateService), and enqueuing the
    RESUME_PARSE background task (Phase 8) to actually parse the file.
    """

    def __init__(
        self,
        resume_service: ResumeUploadService,
        campaign_candidate_service: CampaignCandidateService,
        campaign_repo: CampaignRepository,
        audit_service: AuditService,
        task_log_repo: CeleryTaskLogRepository,
    ):
        self.resume_service = resume_service
        self.campaign_candidate_service = campaign_candidate_service
        self.campaign_repo = campaign_repo
        self.audit_service = audit_service
        self.task_log_repo = task_log_repo
        self.task_log_service = CeleryTaskLogService(task_log_repo)

    def upload_resume(
        self,
        campaign_id: UUID,
        file_bytes: bytes,
        filename: str,
        candidate_full_name: str,
        candidate_email: str,
        jurisdiction: str,
        uploaded_by: str,
        actor_role: str | None = None,
        content_type: str | None = None,
        candidate_phone: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        resolution: str | None = None,
    ) -> tuple[Resume, CampaignCandidateResponse, HiringCampaign, UUID | None, bool]:
        campaign = self._precheck_campaign_eligibility(campaign_id)

        # Fast, non-authoritative — rejects a candidate who already has a
        # campaign_candidates row for this campaign before any file upload
        # or Resume row is created, instead of only being caught afterward
        # by create_campaign_candidate's own identical check (which used to
        # leave an orphaned, unprocessed Resume row behind on every rejection).
        self.campaign_candidate_service.check_no_existing_campaign_membership(
            campaign_id, candidate_email,
        )

        # Epic 3 (M05-E03) Phase C2 — may raise DuplicateResumeFileException
        # (no route-level handling needed; the global ResumeException
        # handler already covers it) when no resolution was given for a
        # detected duplicate.
        result = self.resume_service.upload(
            file_bytes=file_bytes,
            filename=filename,
            candidate_full_name=candidate_full_name,
            candidate_email=candidate_email,
            jurisdiction=jurisdiction,
            consent_source=UPLOAD_CONSENT_SOURCE,
            uploaded_by=uploaded_by,
            actor_role=actor_role,
            content_type=content_type,
            candidate_phone=candidate_phone,
            source_campaign_id=campaign_id,
            ip_address=ip_address,
            user_agent=user_agent,
            resolution=resolution,
        )
        resume = result.resume

        # resume.candidate_id is already correct in every case: the
        # freshly resolved/created candidate (no duplicate), or the
        # matched file's own candidate (either duplicate resolution).
        campaign_candidate = self.campaign_candidate_service.create_campaign_candidate(
            CampaignCandidateCreateRequest(
                campaign_id=campaign_id,
                candidate_id=resume.candidate_id,
                resume_id=resume.id,
            ),
            actor_id=uploaded_by,
            actor_role=actor_role,
        )

        if not result.requires_processing:
            # "use_existing" — nothing was actually (re)uploaded or
            # (re)processed, so no RESUME_UPLOADED audit event and no new
            # Celery task; surface whatever task_id this resume already
            # carries (may be None for older resumes).
            existing_task_id = UUID(resume.task_id) if resume.task_id else None
            return resume, campaign_candidate, campaign, existing_task_id, False

        try:
            self.audit_service.log(
                actor_id=uploaded_by,
                actor_role=None,
                action_type=ActionType.RESUME_UPLOADED,
                entity_type=EntityType.RESUME,
                entity_id=resume.id,
                campaign_id=campaign_id,
                details={
                    "candidate_id": str(resume.candidate_id),
                    "file_format": resume.file_format.value,
                },
            )
            self.campaign_repo.commit()
        except Exception:
            self.campaign_repo.rollback()
            raise

        logger.info("Resume stored | resume_id=%s campaign_id=%s", resume.id, campaign_id)

        task_id = uuid4()
        self.resume_service.record_task_id(resume, str(task_id))

        # Resume-upload resilience: the celery_task_log row is created and
        # committed BEFORE any Celery operation is attempted - the status
        # endpoint (and the recovery job) must always find a row for this
        # task_id, whether or not the broker is reachable right now.
        # Idempotency-key-guarded (same pattern as
        # embedding_tasks._enqueue_resume_embedding) purely as a safety net;
        # resume.id is fresh for every genuinely new upload.
        idempotency_key = f"{RESUME_DOCUMENT_PROCESSING_TASK_TYPE}:{resume.id}"
        task_log, was_created = self.task_log_repo.create_if_new_idempotency_key(
            CeleryTaskLog(
                task_id=str(task_id),
                task_type=RESUME_DOCUMENT_PROCESSING_TASK_TYPE,
                idempotency_key=idempotency_key,
                resume_id=resume.id,
                status=TaskStatus.QUEUED,
            ),
        )
        self.task_log_repo.commit()

        if not was_created:
            # Lost a race against another request for this exact resume_id -
            # reuse the winner's row/task_id rather than returning a task_id
            # with no matching celery_task_log row. resume.task_id was
            # already persisted above using the LOSING task_id (before this
            # check could run) - it must be corrected to the winner's
            # task_id too, or resumes.task_id and celery_task_log.task_id
            # would permanently disagree for this resume.
            task_id = UUID(task_log.task_id)
            self.resume_service.record_task_id(resume, str(task_id))
            logger.info(
                "Task already created for resume_id=%s (race) - reusing task_id=%s", resume.id, task_id,
            )
            return resume, campaign_candidate, campaign, task_id, True

        logger.info("Task created | resume_id=%s task_id=%s", resume.id, task_id)

        try:
            process_resume_document.apply_async(
                kwargs={"resume_id": str(resume.id), "prompt_template_id": str(campaign.prompt_template_id)},
                task_id=str(task_id),
            )
            logger.info("Task queued | resume_id=%s task_id=%s", resume.id, task_id)
        except Exception as exc:
            # Broker/Celery unavailable: never fail the upload, never touch
            # the already-committed resume/candidate/campaign_candidate -
            # celery_task_log stays QUEUED (not a terminal failure) and is
            # picked up by the recovery job once the broker is back.
            logger.exception(
                "Queue unavailable - resume_id=%s task_id=%s", resume.id, task_id,
            )
            self.task_log_service.mark_dispatch_failed(task_log, str(exc))

        return resume, campaign_candidate, campaign, task_id, True

    def _precheck_campaign_eligibility(self, campaign_id: UUID) -> HiringCampaign:
        """
        Fast, non-authoritative check so an obviously-doomed request fails
        before a file is uploaded to storage. Not a substitute for the
        locked, race-safe check inside
        CampaignCandidateService.create_campaign_candidate, which still
        runs afterward as the source of truth.
        """
        campaign = self.campaign_repo.get_by_id(campaign_id)

        if not campaign:
            raise CampaignException("Campaign not found.", 404)

        if campaign.status == CampaignStatus.PAUSED:
            raise CampaignException(
                "This campaign is currently paused — uploads are not accepted.", 409,
            )
        if campaign.status != CampaignStatus.ACTIVE:
            raise CampaignException(
                "This campaign is closed and no longer accepting applications.", 403,
            )

        if campaign.max_candidates:
            current_count = self.campaign_repo.get_candidate_count(campaign_id)
            if current_count >= campaign.max_candidates:
                raise CampaignException(
                    "This campaign has reached its maximum candidate limit.", 409,
                )

        return campaign
