"""
Duplicate resume decisions: what happens when a resume is uploaded to a
campaign that already holds that file, or already holds that candidate.

  identical file already in THIS campaign      skipped (single: 409
                                               DUPLICATE_FILE_IN_CAMPAIGN; bulk:
                                               SKIPPED_DUPLICATE), nothing stored
  same candidate (email) in this campaign,     the uploader decides:
  different file                                 keep existing - the new file is
                                                   discarded
                                                 use new version - see below
  single upload                                decided before anything is written
                                               (409 CANDIDATE_IN_CAMPAIGN_NEW_FILE,
                                               resend with a resolution)
  bulk upload                                  held as an upload_decisions row
                                               (file kept, nothing else changed),
                                               decided later via the API; expires
                                               to "keep existing" after 30 days

"Use new version" (shared by both): the next Resume version is created from
the new file and becomes the candidate's active version (the old one stays as
history), THIS campaign's candidate is pointed at it, the file is parsed by
the normal pipeline in new-version mode (process_resume_document
new_version=...: after the parse only request_rescore runs for this candidate
- a stage only moves by a recruiter's decision), and the candidate's campaign
candidates in other campaigns are stamped "score out of date".
"""
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from app.core.encryption_service import EncryptionService
from app.enums.constants import ActionType, EntityType
from app.exceptions.resume_exceptions import (
    UploadDecisionForbiddenException,
    UploadDecisionNotFoundException,
    UploadDecisionNotPendingException,
)
from app.models.async_tasks import BulkUploadFileStatus, CeleryTaskLog, TaskStatus
from app.models.candidates import FileFormat, ParseStatus, Resume
from app.models.identity import UserRole
from app.models.upload_decisions import (
    DECISION_TTL_DAYS,
    UploadDecision,
    UploadDecisionChoice,
    UploadDecisionStatus,
)
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.audit_repository import AuditRepository
from app.repositories.bulk_upload_job_file_repository import BulkUploadJobFileRepository
from app.repositories.bulk_upload_job_repository import BulkUploadJobRepository
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.candidate_repository import CandidateRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.encryption_key_repository import EncryptionKeyRepository
from app.repositories.resume_repository import ResumeRepository
from app.repositories.upload_decision_repository import UploadDecisionRepository
from app.schemas.resume.upload_decision import (
    NEW_FILE_RESOLUTIONS,
    CandidateInCampaignNewFileResponse,
    DuplicateFileInCampaignResponse,
    UploadDecisionDecideResponse,
    UploadDecisionItem,
)
from app.services.audit_service import AuditService
from app.services.campaign.score_out_of_date import stamp_other_campaigns_for_new_version

logger = logging.getLogger(__name__)

RESUME_STORAGE_BUCKET = "airs_resumes"
SYSTEM_ACTOR_ROLE = "SYSTEM"


def decision_actor_role(actor_roles: list[str] | None, *, is_uploader: bool) -> str | None:
    """
    The role that authorised a decision, for its audit entry: HR_ADMIN when
    the user holds it (any decision), else RECRUITER for the uploader - never
    the first entry on the sign-in (some tokens list "General" first).
    """
    from app.middleware.rbac import authorising_role
    if UserRole.HR_ADMIN.value in (actor_roles or []):
        return UserRole.HR_ADMIN.value
    return authorising_role(actor_roles, (UserRole.RECRUITER,)) if is_uploader else None


def mask_email(email: str) -> str:
    """Same masking as the talent pool: first character of the local part and the full domain."""
    local, _, domain = email.partition("@")
    if not domain or not local:
        return "***"
    return f"{local[0]}{'*' * max(len(local) - 1, 3)}@{domain}"


class UploadDecisionService:
    def __init__(self, db, storage_service=None, encryption_service: EncryptionService | None = None):
        self.db = db
        self.resume_repo = ResumeRepository(db)
        self.campaign_candidate_repo = CampaignCandidateRepository(db)
        self.campaign_repo = CampaignRepository(db)
        self.candidate_repo = CandidateRepository(db)
        self.decision_repo = UploadDecisionRepository(db)
        self.job_repo = BulkUploadJobRepository(db)
        self.job_file_repo = BulkUploadJobFileRepository(db)
        self.task_log_repo = CeleryTaskLogRepository(db)
        self.audit_service = AuditService(AuditRepository(db))
        self.encryption_service = encryption_service or EncryptionService(EncryptionKeyRepository(db))
        self._storage_service = storage_service

    @property
    def storage_service(self):
        # Created on first use: listing decisions never touches storage.
        if self._storage_service is None:
            from app.core.storage_service import StorageService
            self._storage_service = StorageService()
        return self._storage_service

    # --- lookups (upload time) ------------------------------------------------
    def find_identical_file_in_campaign(self, campaign_id: UUID, file_hash: str):
        return self.campaign_candidate_repo.get_by_campaign_and_file_hash(campaign_id, file_hash)

    def find_candidate_in_campaign(self, campaign_id: UUID, email: str):
        candidate = self.candidate_repo.get_by_email_hash(EncryptionService.generate_hash(email))
        if candidate is None:
            return None
        return self.campaign_candidate_repo.get_by_campaign_and_candidate(campaign_id, candidate.id)

    def find_candidate_in_campaign_by_file(self, campaign_id: UUID, file_hash: str):
        """
        A candidate already in this campaign who owns a resume with this exact
        file (any of their versions) - whatever email was typed. Without this,
        "upload anyway" made a new version under that candidate, then failed to
        add them to the campaign they were already in (an orphaned version).
        """
        from sqlalchemy import select
        from app.models.pipeline import CampaignCandidate
        return self.db.execute(
            select(CampaignCandidate)
            .join(Resume, Resume.candidate_id == CampaignCandidate.candidate_id)
            .where(CampaignCandidate.campaign_id == campaign_id, Resume.file_hash == file_hash)
            .limit(1)
        ).scalars().first()

    def duplicate_file_in_campaign_data(self, cc) -> DuplicateFileInCampaignResponse:
        resume = self.resume_repo.get_by_id(cc.resume_id)
        return DuplicateFileInCampaignResponse(
            campaign_candidate_id=cc.id,
            candidate_id=cc.candidate_id,
            candidate_name=self._candidate_name(cc.candidate_id),
            current_pipeline_stage=cc.pipeline_stage.value,
            existing_resume_id=cc.resume_id,
            existing_original_filename=resume.original_filename if resume else None,
            existing_uploaded_at=resume.created_at if resume else None,
        )

    def candidate_in_campaign_data(self, cc) -> CandidateInCampaignNewFileResponse:
        resume = self.resume_repo.get_by_id(cc.resume_id)
        return CandidateInCampaignNewFileResponse(
            campaign_candidate_id=cc.id,
            candidate_id=cc.candidate_id,
            candidate_name=self._candidate_name(cc.candidate_id),
            current_pipeline_stage=cc.pipeline_stage.value,
            existing_resume_id=cc.resume_id,
            existing_original_filename=resume.original_filename if resume else None,
            existing_uploaded_at=resume.created_at if resume else None,
            existing_version_number=resume.version_number if resume else None,
            available_resolutions=list(NEW_FILE_RESOLUTIONS),
        )

    # --- audit (upload time) --------------------------------------------------
    def log_duplicate_skipped(self, cc, *, filename: str | None, file_hash: str, actor_id: str | None,
                              actor_role: str | None, bulk_upload_job_id: UUID | None = None,
                              source: str = "single_upload") -> None:
        """Audit only - the caller commits. source: single_upload, or bulk_upload_worker."""
        self.audit_service.log(
            actor_id=actor_id, actor_role=actor_role, action_type=ActionType.RESUME_DUPLICATE_SKIPPED,
            entity_type=EntityType.CAMPAIGN_CANDIDATE, entity_id=cc.id, campaign_id=cc.campaign_id,
            details={"existing_resume_id": str(cc.resume_id), "file_hash": file_hash,
                     "bulk_upload_job_id": str(bulk_upload_job_id) if bulk_upload_job_id else None,
                     "source": source},
        )

    def uploader_role(self, user_id: str | None) -> str | None:
        """The uploader's role on their local user record - what a worker-written audit entry records."""
        if not user_id:
            return None
        from app.models.identity import User
        user = self.db.get(User, user_id)
        role = getattr(user, "role", None)
        return getattr(role, "value", role)

    def keep_existing_for_upload(self, cc, *, filename: str | None, actor_id: str, actor_role: str | None) -> None:
        """Single upload, resolution=keep_existing: nothing was stored - audited and committed."""
        try:
            self.audit_service.log(
                actor_id=actor_id, actor_role=actor_role, action_type=ActionType.RESUME_KEPT_EXISTING,
                entity_type=EntityType.CAMPAIGN_CANDIDATE, entity_id=cc.id, campaign_id=cc.campaign_id,
                details={"existing_resume_id": str(cc.resume_id),
                         "source": "single_upload"},
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        logger.info("Kept the existing resume | campaign_candidate_id=%s by=%s", cc.id, actor_id)

    # --- use new version (single upload and bulk decision) --------------------
    def use_new_version(self, cc, campaign, *, storage_path: str, file_format: FileFormat, file_hash: str,
                        original_filename: str | None, file_size_bytes: int | None, page_count: int | None,
                        uploaded_by: str, actor_id: str, actor_role: str | None,
                        bulk_upload_job_id: UUID | None = None,
                        decision: UploadDecision | None = None) -> tuple[Resume, Resume | None, UUID]:
        """
        Creates the next version from the stored file, points this campaign's
        candidate at it, stamps the other campaigns, audits, commits, then
        dispatches the new-version parse. Returns (new_resume, previous_resume,
        task_id). The stage is never touched.
        """
        from app.tasks.resume_processing_tasks import (
            RESUME_DOCUMENT_PROCESSING_TASK_TYPE,
            new_version_idempotency_key,
            process_resume_document,
        )

        previous = self.resume_repo.get_by_id(cc.resume_id) if cc.resume_id else None
        task_id = uuid4()
        try:
            if self.resume_repo.get_active_by_candidate(cc.candidate_id) is None:
                version_number = 1
            else:
                version_number = self.resume_repo.get_max_version_number(cc.candidate_id) + 1
                self.resume_repo.deactivate_active_version(cc.candidate_id)

            resume = self.resume_repo.create(Resume(
                candidate_id=cc.candidate_id,
                file_path=storage_path,
                file_format=file_format,
                file_hash=file_hash,
                original_filename=original_filename,
                file_size_bytes=file_size_bytes,
                version_number=version_number,
                is_active_version=True,
                page_count=page_count,
                ocr_used=False,
                parse_status=ParseStatus.PENDING,
                uploaded_by=uploaded_by,
                bulk_upload_job_id=bulk_upload_job_id,
                task_id=str(task_id),
            ))
            cc.resume_id = resume.id
            stamped = stamp_other_campaigns_for_new_version(self.campaign_candidate_repo, cc.candidate_id, campaign)

            details = {
                "campaign_candidate_id": str(cc.id),
                "old_resume_id": str(previous.id) if previous else None,
                "old_version_number": previous.version_number if previous else None,
                "new_resume_id": str(resume.id),
                "new_version_number": resume.version_number,
                "other_campaigns_marked_out_of_date": stamped,
                "bulk_upload_job_id": str(bulk_upload_job_id) if bulk_upload_job_id else None,
            }
            self.audit_service.log(
                actor_id=actor_id, actor_role=actor_role, action_type=ActionType.RESUME_NEW_VERSION_USED,
                entity_type=EntityType.UPLOAD_DECISION if decision else EntityType.CAMPAIGN_CANDIDATE,
                entity_id=decision.id if decision else cc.id, campaign_id=campaign.id, details=details,
            )
            if decision is not None:
                self._close_decision(decision, UploadDecisionStatus.NEW_VERSION_USED, actor_id, new_resume_id=resume.id)
                self._resolve_job_file(
                    decision, BulkUploadFileStatus.PROCESSED,
                    f"New resume version {resume.version_number} used for the candidate already in this campaign.",
                )

            # The parse task's log row exists (committed) before any dispatch,
            # as for every upload; its idempotency key carries the new-version
            # mode so a recovery redispatch keeps it.
            task_log = CeleryTaskLog(
                task_id=str(task_id),
                task_type=RESUME_DOCUMENT_PROCESSING_TASK_TYPE,
                idempotency_key=new_version_idempotency_key(resume.id, cc.id),
                resume_id=resume.id,
                status=TaskStatus.QUEUED,
            )
            self.task_log_repo.create(task_log)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        logger.info(
            "New resume version used | campaign_candidate_id=%s old_resume_id=%s new_resume_id=%s version=%s by=%s",
            cc.id, previous.id if previous else None, resume.id, resume.version_number, actor_id,
        )
        self._notify(campaign.id, cc.id, resume.id)

        try:
            process_resume_document.apply_async(
                kwargs={"resume_id": str(resume.id), "prompt_template_id": str(campaign.prompt_template_id),
                        "new_version": {"campaign_candidate_id": str(cc.id)}},
                task_id=str(task_id),
            )
        except Exception as exc:
            # Same as a normal upload: the row stays QUEUED, flagged for the
            # recovery job, which redispatches it in new-version mode.
            logger.exception("Queue unavailable - new version resume_id=%s task_id=%s", resume.id, task_id)
            from app.services.celery_task_log_service import CeleryTaskLogService
            CeleryTaskLogService(self.task_log_repo).mark_dispatch_failed(task_log, str(exc))
        return resume, previous, task_id

    # --- bulk: hold a file for a decision -------------------------------------
    def hold_for_decision(self, *, job, job_file, cc, file_hash: str, file_format: FileFormat,
                          file_size_bytes: int | None, page_count: int | None) -> UploadDecision:
        """Flush only - the bulk task commits it with the file status and job counter."""
        now = datetime.now(timezone.utc)
        return self.decision_repo.create(UploadDecision(
            campaign_id=job.campaign_id,
            campaign_candidate_id=cc.id,
            candidate_id=cc.candidate_id,
            existing_resume_id=cc.resume_id,
            bulk_upload_job_id=job.id,
            bulk_upload_job_file_id=job_file.id,
            storage_path=job_file.storage_path,
            original_filename=job_file.original_filename,
            file_hash=file_hash,
            file_format=file_format.value,
            file_size_bytes=file_size_bytes,
            page_count=page_count,
            uploaded_by=job.uploaded_by,
            status=UploadDecisionStatus.PENDING.value,
            expires_at=now + timedelta(days=DECISION_TTL_DAYS),
        ))

    # --- read (API) -----------------------------------------------------------
    def list_for_campaign(self, campaign_id: UUID, status: str | None) -> list[UploadDecisionItem]:
        return [self._to_item(d) for d in self.decision_repo.list_by_campaign(campaign_id, status)]

    def list_for_job(self, bulk_upload_job_id: UUID) -> list[UploadDecisionItem]:
        return [self._to_item(d) for d in self.decision_repo.list_by_bulk_upload_job(bulk_upload_job_id)]

    # --- decide (API) -----------------------------------------------------------
    def decide(self, decision_id: UUID, choice: str, *, actor_id: str, actor_roles: list[str]
               ) -> UploadDecisionDecideResponse:
        decision = self.decision_repo.get_by_id_for_update(decision_id)
        if decision is None:
            raise UploadDecisionNotFoundException()
        if decision.uploaded_by != actor_id and UserRole.HR_ADMIN.value not in (actor_roles or []):
            self.db.rollback()
            raise UploadDecisionForbiddenException(
                "Only the person who uploaded this file or an HR admin can decide."
            )
        if decision.status != UploadDecisionStatus.PENDING.value:
            self.db.rollback()
            raise UploadDecisionNotPendingException(
                f"This upload was already decided ({decision.status})."
            )
        actor_role = decision_actor_role(actor_roles, is_uploader=decision.uploaded_by == actor_id)

        if choice == UploadDecisionChoice.KEEP_EXISTING.value:
            self._keep_existing(decision, UploadDecisionStatus.KEPT_EXISTING, ActionType.RESUME_KEPT_EXISTING,
                                actor_id=actor_id, actor_role=actor_role)
            return UploadDecisionDecideResponse(decision=self._to_item(decision))

        cc = (self.campaign_candidate_repo.get_by_id(decision.campaign_candidate_id)
              if decision.campaign_candidate_id else None)
        campaign = self.campaign_repo.get_by_id(decision.campaign_id)
        if cc is None or campaign is None or cc.campaign_id != decision.campaign_id:
            self.db.rollback()
            raise UploadDecisionNotPendingException(
                "The candidate is no longer in this campaign - only 'keep existing' is possible."
            )
        resume, _previous, task_id = self.use_new_version(
            cc, campaign,
            storage_path=decision.storage_path,
            file_format=FileFormat(decision.file_format),
            file_hash=decision.file_hash,
            original_filename=decision.original_filename,
            file_size_bytes=decision.file_size_bytes,
            page_count=decision.page_count,
            uploaded_by=decision.uploaded_by,
            actor_id=actor_id,
            actor_role=actor_role,
            bulk_upload_job_id=decision.bulk_upload_job_id,
            decision=decision,
        )
        return UploadDecisionDecideResponse(decision=self._to_item(decision), new_resume_id=resume.id,
                                            task_id=task_id)

    # --- expiry (Celery beat) ---------------------------------------------------
    def expire_pending(self, now: datetime | None = None) -> int:
        """Every PENDING decision past expires_at -> keep existing (file discarded), audited as SYSTEM."""
        now = now or datetime.now(timezone.utc)
        expired = 0
        for decision in self.decision_repo.list_expired_pending(now):
            try:
                self._keep_existing(decision, UploadDecisionStatus.EXPIRED_KEPT_EXISTING,
                                    ActionType.UPLOAD_DECISION_EXPIRED, actor_id=None, actor_role=SYSTEM_ACTOR_ROLE)
                expired += 1
            except Exception:
                logger.exception("Could not expire upload decision %s", decision.id)
                self.db.rollback()
        return expired

    # --- candidate removed / erased ---------------------------------------------------
    def close_pending_for_removed_campaign_candidate(self, campaign_candidate_id: UUID, *, actor_id: str | None,
                                                     actor_role: str | None) -> list[str]:
        """
        The candidate was removed from the campaign: each PENDING decision for
        them there becomes KEPT_EXISTING (row kept as history, audited with
        reason "candidate removed from campaign", bulk file resolved). Flush
        only - the caller commits, then deletes the returned held files with
        delete_held_files.
        """
        paths = []
        for decision in self.decision_repo.list_pending_by_campaign_candidate(campaign_candidate_id):
            self._close_decision(decision, UploadDecisionStatus.KEPT_EXISTING, actor_id)
            self._resolve_job_file(
                decision, BulkUploadFileStatus.SKIPPED_DUPLICATE,
                "Candidate removed from this campaign - the new file was discarded.",
            )
            self.audit_service.log(
                actor_id=actor_id, actor_role=actor_role, action_type=ActionType.RESUME_KEPT_EXISTING,
                entity_type=EntityType.UPLOAD_DECISION, entity_id=decision.id, campaign_id=decision.campaign_id,
                details={"campaign_candidate_id": str(campaign_candidate_id),
                         "reason_code": "CANDIDATE_REMOVED_FROM_CAMPAIGN"},
            )
            paths.append(decision.storage_path)
        return paths

    def discard_for_erased_candidate(self, candidate_id: UUID) -> list[str]:
        """
        Candidate erasure: every decision row for the candidate (any status) is
        deleted - no trace, like the rest of their data; a PENDING one's bulk
        file is resolved first. Returns the PENDING decisions' held files (the
        others were already deleted, or became a resume version that erasure
        deletes itself). Flush only - the caller commits and deletes the files.
        """
        paths = []
        for decision in self.decision_repo.list_by_candidate(candidate_id):
            if decision.status == UploadDecisionStatus.PENDING.value:
                self._resolve_job_file(
                    decision, BulkUploadFileStatus.SKIPPED_DUPLICATE,
                    "Candidate data erased - the new file was discarded.",
                )
                paths.append(decision.storage_path)
            self.decision_repo.delete(decision)
        return paths

    def delete_held_files(self, paths: list[str]) -> None:
        """Best-effort: a storage error is logged, never undoes the committed change."""
        for path in paths:
            try:
                self.storage_service.delete_file(RESUME_STORAGE_BUCKET, path)
            except Exception as exc:
                logger.warning("Could not delete a discarded upload file (%s).", type(exc).__name__)

    # --- internals ----------------------------------------------------------------
    def _keep_existing(self, decision: UploadDecision, status: UploadDecisionStatus, action_type: ActionType,
                       *, actor_id: str | None, actor_role: str | None) -> None:
        """
        Decision closed, the job file resolved, audited and committed; then the
        held file is deleted from storage (best-effort - a storage failure is
        logged, the decision stands). Nothing else was ever created from it.
        """
        storage_path = decision.storage_path
        try:
            self._close_decision(decision, status, actor_id)
            self._resolve_job_file(
                decision, BulkUploadFileStatus.SKIPPED_DUPLICATE,
                "Candidate already in this campaign - kept the existing resume"
                + (" (no decision within 30 days)." if status == UploadDecisionStatus.EXPIRED_KEPT_EXISTING
                   else "."),
            )
            self.audit_service.log(
                actor_id=actor_id, actor_role=actor_role, action_type=action_type,
                entity_type=EntityType.UPLOAD_DECISION, entity_id=decision.id, campaign_id=decision.campaign_id,
                details={"campaign_candidate_id": str(decision.campaign_candidate_id)
                         if decision.campaign_candidate_id else None,
                         "existing_resume_id": str(decision.existing_resume_id)
                         if decision.existing_resume_id else None,
                         "bulk_upload_job_id": str(decision.bulk_upload_job_id)
                         if decision.bulk_upload_job_id else None,
                         "status": status.value},
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        try:
            self.storage_service.delete_file(RESUME_STORAGE_BUCKET, storage_path)
        except Exception:
            logger.exception("Could not delete the discarded upload file | decision_id=%s", decision.id)

    @staticmethod
    def _close_decision(decision: UploadDecision, status: UploadDecisionStatus, actor_id: str | None,
                        new_resume_id: UUID | None = None) -> None:
        decision.status = status.value
        decision.decided_by = actor_id
        decision.decided_at = datetime.now(timezone.utc)
        decision.new_resume_id = new_resume_id

    def _resolve_job_file(self, decision: UploadDecision, status: BulkUploadFileStatus, summary: str) -> None:
        """NEEDS_DECISION -> status, with the job counters moved to match and the file log reason updated."""
        if decision.bulk_upload_job_file_id is None:
            return
        job_file = self.job_file_repo.get_by_id(decision.bulk_upload_job_file_id)
        if job_file is None or job_file.status != BulkUploadFileStatus.NEEDS_DECISION:
            return
        self.job_file_repo.update_status(job_file.id, status)
        self.job_repo.decrement_needs_decision_count(job_file.bulk_upload_job_id)
        if status == BulkUploadFileStatus.PROCESSED:
            self.job_repo.increment_processed_count(job_file.bulk_upload_job_id)
        else:
            self.job_repo.increment_duplicate_count(job_file.bulk_upload_job_id)
        task_log = self.task_log_repo.get_by_task_id(job_file.task_id) if job_file.task_id else None
        if task_log is not None:
            task_log.output_summary = summary

    def _to_item(self, decision: UploadDecision) -> UploadDecisionItem:
        existing = self.resume_repo.get_by_id(decision.existing_resume_id) if decision.existing_resume_id else None
        name, email = self._candidate_identity(decision.candidate_id)
        return UploadDecisionItem(
            id=decision.id,
            campaign_id=decision.campaign_id,
            bulk_upload_job_id=decision.bulk_upload_job_id,
            bulk_upload_job_file_id=decision.bulk_upload_job_file_id,
            campaign_candidate_id=decision.campaign_candidate_id,
            candidate_id=decision.candidate_id,
            candidate_name=name,
            candidate_email_masked=mask_email(email) if email else None,
            existing_resume_id=decision.existing_resume_id,
            existing_original_filename=existing.original_filename if existing else None,
            existing_uploaded_at=existing.created_at if existing else None,
            new_original_filename=decision.original_filename,
            uploaded_by=decision.uploaded_by,
            created_at=decision.created_at,
            expires_at=decision.expires_at,
            status=decision.status,
            decided_by=decision.decided_by,
            decided_at=decision.decided_at,
            new_resume_id=decision.new_resume_id,
        )

    def _candidate_name(self, candidate_id: UUID | None) -> str | None:
        return self._candidate_identity(candidate_id)[0]

    def _candidate_identity(self, candidate_id: UUID | None) -> tuple[str | None, str | None]:
        candidate = self.candidate_repo.get_by_id(candidate_id) if candidate_id else None
        if candidate is None or getattr(candidate, "is_pii_deleted", False):
            return None, None
        try:
            name = self.encryption_service.decrypt(candidate.full_name_encrypted, candidate.encryption_key_id)
            email = self.encryption_service.decrypt(candidate.email_encrypted, candidate.encryption_key_id)
            return name, email
        except Exception:
            logger.exception("Could not decrypt candidate identity | candidate_id=%s", candidate_id)
            return None, None

    @staticmethod
    def _notify(campaign_id, campaign_candidate_id, resume_id) -> None:
        try:
            from app.services.candidate_change_notifier import CandidateChangeNotifier
            CandidateChangeNotifier().updated(campaign_id, campaign_candidate_id, resume_id)
        except Exception:
            logger.exception("Board notify failed | campaign_candidate_id=%s", campaign_candidate_id)
