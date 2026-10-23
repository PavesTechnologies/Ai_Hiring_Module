import logging
from typing import NamedTuple
from uuid import UUID, uuid4

from app.enums.constants import ActionType, EntityType
from app.exceptions.campaign_exceptions import CampaignException
from app.exceptions.resume_exceptions import (
    CandidateInCampaignNewFileException,
    DuplicateFileInCampaignException,
    ResumeException,
)
from app.models.async_tasks import CeleryTaskLog, TaskStatus
from app.models.campaigns import CampaignStatus, HiringCampaign
from app.models.candidates import ParseStatus, Resume
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.schemas.campaign.campaign_candidate_schema import (
    CampaignCandidateCreateRequest,
)
from app.schemas.resume.upload_decision import NEW_FILE_RESOLUTIONS
from app.services.audit_service import AuditService
from app.services.campaign.campaign_candidate_service import CampaignCandidateService
from app.services.campaign.score_out_of_date import stamp_other_campaigns_for_new_version
from app.services.celery_task_log_service import CeleryTaskLogService
from app.services.resume.resume_upload_service import ResumeUploadService
from app.services.resume.upload_decision_service import UploadDecisionService
from app.tasks.resume_processing_tasks import (
    RESUME_DOCUMENT_PROCESSING_TASK_TYPE,
    _enqueue_deterministic_scoring,
    process_resume_document,
)
from app.services.candidate_change_notifier import CandidateChangeNotifier

logger = logging.getLogger(__name__)

UPLOAD_CONSENT_SOURCE = "UPLOAD_FORM"

OUTCOME_UPLOADED = "UPLOADED"
OUTCOME_LINKED_EXISTING = "LINKED_EXISTING"
OUTCOME_KEPT_EXISTING = "KEPT_EXISTING"
OUTCOME_NEW_VERSION_USED = "NEW_VERSION_USED"


class ResumeIntakeResult(NamedTuple):
    resume: Resume
    campaign_candidate: object
    campaign: HiringCampaign
    task_id: UUID | None
    requires_processing: bool
    # OUTCOME_*: what the upload did (see ResumeUploadAcceptedResponse.outcome).
    outcome: str = OUTCOME_UPLOADED
    previous_resume_id: UUID | None = None


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
        upload_decision_service: UploadDecisionService | None = None,
    ):
        self.resume_service = resume_service
        self.campaign_candidate_service = campaign_candidate_service
        self.campaign_repo = campaign_repo
        self.audit_service = audit_service
        self.task_log_repo = task_log_repo
        self.task_log_service = CeleryTaskLogService(task_log_repo)
        # Same session as the repositories above (the request's).
        self.upload_decision_service = upload_decision_service or UploadDecisionService(task_log_repo.db)

    def _queue_scoring_for_existing_resume(self, resume, campaign_candidate_id) -> None:
        """
        A "use existing" candidate is scored like a normal upload, reusing the
        resume's existing parse (never re-parsed): deterministic -> semantic ->
        AI against THIS campaign's JD, plus v2 shadow when the campaign has it
        on - the same chain a finished parse starts (_enqueue_deterministic_
        scoring, idempotent per campaign candidate, so candidates already
        scored in other campaigns are not touched).
        Parse still running: nothing to do - when it finishes it queues every
        campaign candidate on the resume, this one included. Parse failed:
        nothing can be scored; logged. Post-commit and best-effort: a queueing
        failure never undoes the committed candidate.
        """
        status = getattr(resume, "parse_status", None)
        if status != ParseStatus.PARSED:
            logger.info(
                "'Use existing' candidate not queued for scoring yet | campaign_candidate_id=%s parse_status=%s",
                campaign_candidate_id, getattr(status, "value", status),
            )
            return
        try:
            _enqueue_deterministic_scoring(
                self.task_log_repo.db, resume.id, CeleryTaskLogService(self.task_log_repo),
            )
        except Exception:
            logger.exception(
                "Could not queue scoring for 'use existing' candidate | campaign_candidate_id=%s", campaign_candidate_id,
            )

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
    ) -> ResumeIntakeResult:
        campaign = self._precheck_campaign_eligibility(campaign_id)

        # Duplicate resume decisions - both checked before anything is stored:
        # 1. this exact file is already a candidate's resume in this campaign:
        #    skipped (409 DUPLICATE_FILE_IN_CAMPAIGN), never asked about;
        # 2. the candidate (email) is already in this campaign with another
        #    file: the uploader decides keep_existing / use_new_version (409
        #    CANDIDATE_IN_CAMPAIGN_NEW_FILE until a resolution is sent).
        # This replaces the old "Candidate already exists in this campaign"
        # pre-check; create_campaign_candidate's locked check still backs the
        # insert below.
        file_hash = ResumeUploadService._hash_file_bytes(file_bytes)
        identical = self.upload_decision_service.find_identical_file_in_campaign(campaign_id, file_hash)
        if identical is not None:
            self._skip_identical_file(identical, filename, file_hash, uploaded_by, actor_role)

        existing = self.upload_decision_service.find_candidate_in_campaign(campaign_id, candidate_email)
        if existing is not None:
            return self._resolve_candidate_in_campaign(
                existing, campaign, file_bytes=file_bytes, filename=filename, content_type=content_type,
                uploaded_by=uploaded_by, actor_role=actor_role, resolution=resolution,
            )
        if resolution in NEW_FILE_RESOLUTIONS:
            raise ResumeException(
                f"'{resolution}' only applies to a candidate already in this campaign - this one isn't.", 400,
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
            # carries (may be None for older resumes). The new campaign
            # candidate IS committed here (with an audit entry) - before this
            # it was rolled back while the API still answered 201.
            try:
                self.audit_service.log(
                    actor_id=uploaded_by,
                    actor_role=actor_role,
                    action_type=ActionType.RESUME_DUPLICATE_USE_EXISTING,
                    entity_type=EntityType.CAMPAIGN_CANDIDATE,
                    entity_id=campaign_candidate.id,
                    campaign_id=campaign_id,
                    details={"resume_id": str(resume.id), "candidate_id": str(resume.candidate_id)},
                )
                self.campaign_repo.commit()
            except Exception:
                self.campaign_repo.rollback()
                raise
            logger.info(
                "Existing resume added to campaign ('use existing') | resume_id=%s campaign_id=%s "
                "campaign_candidate_id=%s by=%s", resume.id, campaign_id, campaign_candidate.id, uploaded_by,
            )
            self._queue_scoring_for_existing_resume(resume, campaign_candidate.id)
            existing_task_id = UUID(resume.task_id) if resume.task_id else None
            return ResumeIntakeResult(resume, campaign_candidate, campaign, existing_task_id, False,
                                      OUTCOME_LINKED_EXISTING)

        try:
            # A known candidate's new file became their active version: their
            # campaign candidates in other campaigns still score the old one.
            if (getattr(resume, "version_number", 1) or 1) > 1:
                stamp_other_campaigns_for_new_version(
                    self.upload_decision_service.campaign_candidate_repo, resume.candidate_id, campaign,
                )
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

        # WebSocket board update - published only after the commit above,
        # which is what actually persists create_campaign_candidate()'s
        # insert (CampaignCandidateService itself never commits this branch;
        # it shares this request's session, and this is the first commit
        # reached after that insert). Never allowed to fail the upload.
        CandidateChangeNotifier().added(campaign_id, campaign_candidate)

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
            return ResumeIntakeResult(resume, campaign_candidate, campaign, task_id, True)

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

        return ResumeIntakeResult(resume, campaign_candidate, campaign, task_id, True)

    def _skip_identical_file(self, campaign_candidate, filename, file_hash, uploaded_by, actor_role) -> None:
        """Audited (best-effort, committed) and raised as the 409 - nothing is stored."""
        decisions = self.upload_decision_service
        data = decisions.duplicate_file_in_campaign_data(campaign_candidate)
        try:
            decisions.log_duplicate_skipped(
                campaign_candidate, filename=filename, file_hash=file_hash, actor_id=uploaded_by,
                actor_role=actor_role,
            )
            self.campaign_repo.commit()
        except Exception:
            logger.exception("Could not audit the skipped duplicate | campaign_candidate_id=%s", campaign_candidate.id)
            self.campaign_repo.rollback()
        logger.info(
            "Identical file already in campaign - skipped | campaign_candidate_id=%s by=%s",
            campaign_candidate.id, uploaded_by,
        )
        owner = data.candidate_name or "another candidate"
        raise DuplicateFileInCampaignException(
            f"This exact file is already in this campaign as {owner}'s resume - nothing was uploaded.",
            data=data,
        )

    def _resolve_candidate_in_campaign(
        self, campaign_candidate, campaign, *, file_bytes, filename, content_type, uploaded_by, actor_role,
        resolution,
    ) -> ResumeIntakeResult:
        decisions = self.upload_decision_service
        if resolution not in NEW_FILE_RESOLUTIONS:
            raise CandidateInCampaignNewFileException(
                "This candidate is already in this campaign with a different resume. Keep the existing "
                "resume, or use the new file as their new resume version.",
                data=decisions.candidate_in_campaign_data(campaign_candidate),
            )

        if resolution == "keep_existing":
            decisions.keep_existing_for_upload(
                campaign_candidate, filename=filename, actor_id=uploaded_by, actor_role=actor_role,
            )
            resume = decisions.resume_repo.get_by_id(campaign_candidate.resume_id)
            task_id = UUID(resume.task_id) if resume is not None and resume.task_id else None
            return ResumeIntakeResult(resume, campaign_candidate, campaign, task_id, False, OUTCOME_KEPT_EXISTING)

        object_path, validation_result, file_hash = self.resume_service.store_file(
            file_bytes, filename, content_type,
        )
        try:
            resume, previous, task_id = decisions.use_new_version(
                campaign_candidate, campaign,
                storage_path=object_path,
                file_format=validation_result.file_format,
                file_hash=file_hash,
                original_filename=filename,
                file_size_bytes=len(file_bytes),
                page_count=None,
                uploaded_by=uploaded_by,
                actor_id=uploaded_by,
                actor_role=actor_role,
            )
        except Exception:
            # Nothing was committed - don't leave the stored file behind.
            try:
                self.resume_service.storage_service.delete_file(
                    ResumeUploadService.RESUME_STORAGE_BUCKET, object_path,
                )
            except Exception:
                logger.exception("Could not delete the stored file after a failed new version | %s", object_path)
            raise
        return ResumeIntakeResult(
            resume, campaign_candidate, campaign, task_id, True, OUTCOME_NEW_VERSION_USED,
            previous.id if previous is not None else None,
        )

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

        # Deliberately no max_candidates check here: max_candidates is an
        # openings count, consumed when a candidate reaches SELECTED, not an
        # intake limit. Screening many resumes to fill a few positions is the
        # normal case, so uploads are never capped by it.
        return campaign
