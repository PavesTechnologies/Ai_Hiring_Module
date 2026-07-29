from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from app.core.storage_service import StorageService
from app.enums.constants import ActionType, EntityType
from app.exception_handler.exceptions import NotFoundError
from app.exceptions.bulk_upload_exceptions import (
    BulkUploadFileNotReplayableException,
    BulkUploadJobNotCancellableException,
    BulkUploadJobNotFoundException,
)
from app.exceptions.campaign_exceptions import CampaignException
from app.models.async_tasks import BulkUploadFileStatus, BulkUploadJob, BulkUploadJobFile, BulkUploadStatus, CeleryTaskLog
from app.models.campaigns import CampaignStatus, HiringCampaign
from app.repositories.bulk_upload_job_file_repository import BulkUploadJobFileRepository
from app.repositories.bulk_upload_job_repository import BulkUploadJobRepository
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.dead_letter_queue_repository import DeadLetterQueueRepository
from app.schemas.bulk_upload.response import BulkUploadFileLogEntry, BulkUploadFileLogResult
from app.services.audit_service import AuditService
from app.services.bulk_upload.zip_validation_service import ZipValidationService
from app.tasks.bulk_upload_tasks import extract_bulk_upload_zip, parse_bulk_upload_file
from app.utils.excel_export import ExcelExport

_NON_REPLAYABLE_JOB_STATUSES = (BulkUploadStatus.PENDING, BulkUploadStatus.EXTRACTING, BulkUploadStatus.CANCELLED)

_TERMINAL_JOB_STATUSES = (
    BulkUploadStatus.COMPLETED,
    BulkUploadStatus.PARTIAL_FAILURE,
    BulkUploadStatus.FAILED,
    BulkUploadStatus.CANCELLED,
)

_TERMINAL_FILE_STATUSES = (
    BulkUploadFileStatus.PROCESSED,
    BulkUploadFileStatus.FAILED,
    BulkUploadFileStatus.CANCELLED,
)

# Epic 4 (M05-E04) Phase D5 — the exact wording C3 (Epic 3) writes to
# celery_task_log.output_summary for an auto-skipped exact-duplicate file
# (bulk_upload_tasks.py). Both a duplicate and a genuine success share
# BulkUploadFileStatus.PROCESSED — this marker is the only signal that
# tells them apart. Encapsulated here, in one place, precisely because
# it's a fragile coupling to another phase's literal string.
_DUPLICATE_FILE_OUTPUT_SUMMARY_MARKER = "Duplicate file detected"

_DEFAULT_FAILURE_REASON = "Processing failed for an unspecified reason."
_SKIPPED_FILE_REASON = "Job was cancelled before this file could be processed."


class BulkUploadService:
    """
    Validates the ZIP, stores it, creates the bulk_upload_jobs record at
    status=PENDING, and enqueues the BULK_EXTRACT task (Phase B3) which
    unpacks the archive asynchronously. No per-file parsing happens here
    or in BULK_EXTRACT itself — that's Phase B4. Also supports cancelling
    a job that hasn't reached a terminal state yet (Phase B7).
    """

    BULK_UPLOAD_STORAGE_BUCKET = "airs_resumes"
    # Sentinel for audit_log.entity_id on a history export — there's no
    # single job the action is "about", only a campaign-scoped list.
    # Mirrors JDService.EXPORT_AUDIT_ENTITY_ID exactly.
    EXPORT_AUDIT_ENTITY_ID = UUID("00000000-0000-0000-0000-000000000000")

    def __init__(
        self,
        bulk_upload_job_repo: BulkUploadJobRepository,
        bulk_upload_job_file_repo: BulkUploadJobFileRepository,
        zip_validation_service: ZipValidationService,
        storage_service: StorageService,
        campaign_repo: CampaignRepository,
        audit_service: AuditService,
        celery_task_log_repo: CeleryTaskLogRepository,
        dead_letter_queue_repo: DeadLetterQueueRepository,
    ):
        self.bulk_upload_job_repo = bulk_upload_job_repo
        self.bulk_upload_job_file_repo = bulk_upload_job_file_repo
        self.zip_validation_service = zip_validation_service
        self.storage_service = storage_service
        self.campaign_repo = campaign_repo
        self.audit_service = audit_service
        self.celery_task_log_repo = celery_task_log_repo
        self.dead_letter_queue_repo = dead_letter_queue_repo

    def upload_zip(
        self,
        campaign_id: UUID,
        file_bytes: bytes,
        filename: str,
        uploaded_by: str,
        consent_confirmed: bool,
        jurisdiction: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[BulkUploadJob, HiringCampaign, UUID]:
        campaign = self._precheck_campaign_eligibility(campaign_id)

        self.zip_validation_service.validate(file_bytes, filename)

        object_path = self._build_object_path(campaign_id)
        self.storage_service.upload_file(
            bucket_name=self.BULK_UPLOAD_STORAGE_BUCKET,
            file_path=object_path,
            file_content=file_bytes,
            content_type="application/zip",
        )

        job = BulkUploadJob(
            campaign_id=campaign_id,
            uploaded_by=uploaded_by,
            original_filename=filename,
            zip_storage_path=object_path,
            consent_confirmed=consent_confirmed,
            jurisdiction=jurisdiction,
            ip_address=ip_address,
            user_agent=user_agent,
            status=BulkUploadStatus.PENDING,
        )

        try:
            job = self.bulk_upload_job_repo.create(job)
            self.bulk_upload_job_repo.commit()
        except Exception:
            self.bulk_upload_job_repo.rollback()
            raise

        task_id = uuid4()
        extract_bulk_upload_zip.apply_async(
            kwargs={"task_id": str(task_id), "bulk_upload_job_id": str(job.id)},
            task_id=str(task_id),
        )

        return job, campaign, task_id

    def _precheck_campaign_eligibility(self, campaign_id: UUID) -> HiringCampaign:
        """
        Fast, non-authoritative check mirroring
        ResumeIntakeService._precheck_campaign_eligibility exactly — the
        authoritative per-file cap enforcement happens during extraction
        (Phase B5), since only then is the archive's actual file count known.
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

    def _build_object_path(self, campaign_id: UUID) -> str:
        return f"campaign_{campaign_id}/bulk-zip/{uuid4()}.zip"

    def cancel_job(
        self,
        job_id: UUID,
        actor_id: str,
        actor_role: str | None = None,
    ) -> tuple[BulkUploadJob, int]:
        """
        Soft-cancels a bulk upload job — mirrors CampaignRepository's
        pause/suspend pattern: a bulk status flip on still-QUEUED work,
        with anything already RUNNING left to finish naturally (no real
        Celery-level task revocation exists anywhere in this codebase).
        """
        job = self.bulk_upload_job_repo.get_by_id(job_id)
        if job is None:
            raise BulkUploadJobNotFoundException("Bulk upload job not found.")

        if job.status in _TERMINAL_JOB_STATUSES:
            raise BulkUploadJobNotCancellableException(
                f"This bulk upload is already {job.status.value.lower()} and cannot be cancelled."
            )

        previous_status = job.status.value

        try:
            files_cancelled = self.bulk_upload_job_file_repo.cancel_queued_files(job_id)
            self.bulk_upload_job_repo.update_status(
                job_id, BulkUploadStatus.CANCELLED, completed_at=datetime.now(timezone.utc),
            )

            self.audit_service.log(
                actor_id=actor_id,
                actor_role=actor_role,
                action_type=ActionType.BULK_UPLOAD_CANCELLED,
                entity_type=EntityType.BULK_UPLOAD_JOB,
                entity_id=job.id,
                campaign_id=job.campaign_id,
                details={
                    "previous_status": previous_status,
                    "files_cancelled": files_cancelled,
                },
            )
            self.bulk_upload_job_repo.commit()
        except Exception:
            self.bulk_upload_job_repo.rollback()
            raise

        job = self.bulk_upload_job_repo.get_by_id(job_id)
        return job, files_cancelled

    def replay_failed_file(
        self,
        job_id: UUID,
        file_id: UUID,
        actor_id: str,
        actor_role: str | None = None,
    ) -> tuple[BulkUploadJobFile, str]:
        """
        Re-enqueues a single permanently-failed file's BULK_RESUME_PARSE
        task under a fresh task_id. Only files that actually reached the
        dead letter queue are replayable — a duplicate-candidate or
        unexpected-exception failure is a deterministic outcome (per
        parse_bulk_upload_file's own docstring: "retrying those could never
        succeed differently") and never creates a DLQ row in the first
        place, so there is nothing safe to replay for those.
        """
        job = self.bulk_upload_job_repo.get_by_id(job_id)
        if job is None:
            raise BulkUploadJobNotFoundException("Bulk upload job not found.")
        if job.status in _NON_REPLAYABLE_JOB_STATUSES:
            raise BulkUploadFileNotReplayableException(
                f"This bulk upload job is {job.status.value.lower()} and cannot accept a replay."
            )

        job_file = self.bulk_upload_job_file_repo.get_by_id_and_job(file_id, job_id)
        if job_file is None:
            raise NotFoundError(f"File {file_id} not found in bulk upload job {job_id}.")
        if job_file.status != BulkUploadFileStatus.FAILED:
            raise BulkUploadFileNotReplayableException(
                f"Only a FAILED file can be replayed (current status: {job_file.status.value})."
            )

        campaign = self.campaign_repo.get_by_id(job.campaign_id)
        if campaign is None:
            raise CampaignException("Campaign not found.", 404)
        if campaign.status == CampaignStatus.PAUSED:
            raise CampaignException(
                "This campaign is currently paused — replay is not allowed.", 409,
            )
        if campaign.status != CampaignStatus.ACTIVE:
            raise CampaignException(
                "This campaign is closed and no longer accepting applications.", 403,
            )

        dlq_entry = (
            self.dead_letter_queue_repo.get_by_task_id(job_file.task_id) if job_file.task_id else None
        )
        if dlq_entry is None:
            raise BulkUploadFileNotReplayableException(
                "This file's failure was never dead-lettered and cannot be replayed."
            )

        original_task_id = job_file.task_id
        new_task_id = str(uuid4())

        try:
            requeued = self.bulk_upload_job_file_repo.requeue_for_replay(file_id, new_task_id)
            if not requeued:
                raise BulkUploadFileNotReplayableException(
                    "This file is no longer FAILED — it may already be replaying."
                )

            self.bulk_upload_job_repo.decrement_failed_count(job_id)
            if job.status != BulkUploadStatus.PROCESSING:
                self.bulk_upload_job_repo.requeue_after_replay(job_id)

            self.dead_letter_queue_repo.mark_replayed(
                dlq_entry.id, replayed_by=actor_id, replayed_at=datetime.now(timezone.utc),
            )

            self.audit_service.log(
                actor_id=actor_id,
                actor_role=actor_role,
                action_type=ActionType.BULK_UPLOAD_FILE_REPLAYED,
                entity_type=EntityType.BULK_UPLOAD_JOB_FILE,
                entity_id=job_file.id,
                campaign_id=job.campaign_id,
                details={
                    "bulk_upload_job_id": str(job_id),
                    "original_filename": job_file.original_filename,
                    "original_task_id": original_task_id,
                    "new_task_id": new_task_id,
                    "dead_letter_queue_id": str(dlq_entry.id),
                },
            )
            self.bulk_upload_job_repo.commit()
        except Exception:
            self.bulk_upload_job_repo.rollback()
            raise

        parse_bulk_upload_file.apply_async(
            kwargs={"task_id": new_task_id, "bulk_upload_job_file_id": str(file_id)},
            task_id=new_task_id,
        )

        job_file = self.bulk_upload_job_file_repo.get_by_id(file_id)
        return job_file, new_task_id

    def get_job_progress(self, job_id: UUID) -> tuple[BulkUploadJob, float, int, datetime | None]:
        """
        Epic 4 (M05-E04) Phase D4 — lightweight polling data: percent
        complete, remaining count, and a linear ETA. Deliberately separate
        from get_job_detail, which also fetches the full per-file list —
        unsuited to a frequent 10s poll. Returns
        (job, percent_complete, remaining_count, estimated_completion_at).
        """
        job = self.bulk_upload_job_repo.get_by_id(job_id)
        if job is None:
            raise BulkUploadJobNotFoundException("Bulk upload job not found.")

        resolved_count = job.processed_count + job.failed_count + job.duplicate_count
        remaining_count = max(job.total_files - resolved_count, 0)

        if job.total_files > 0:
            percent_complete = min(round(resolved_count / job.total_files * 100, 1), 100.0)
        else:
            percent_complete = 0.0

        estimated_completion_at = None
        if job.status == BulkUploadStatus.PROCESSING and resolved_count > 0:
            # Prefer the earliest real task started_at over created_at
            # (job-row-insertion time, before any worker has actually
            # picked anything up) — falls back to created_at when no
            # task has recorded a started_at yet.
            processing_started_at = (
                self.celery_task_log_repo.get_earliest_started_at_by_bulk_upload_job_id(job_id)
                or job.created_at
            )
            now = datetime.now(timezone.utc)
            elapsed_seconds = (now - processing_started_at).total_seconds()
            if elapsed_seconds > 0:
                rate_per_second = resolved_count / elapsed_seconds
                if rate_per_second > 0:
                    eta_seconds = remaining_count / rate_per_second
                    estimated_completion_at = now + timedelta(seconds=eta_seconds)

        return job, percent_complete, remaining_count, estimated_completion_at

    def get_file_log(
        self, job_id: UUID, *, limit: int = 50, offset: int = 0,
    ) -> tuple[list[BulkUploadFileLogEntry], int]:
        """
        Epic 4 (M05-E04) Phase D5 — live, most-recently-resolved-first log
        of every file that has reached a terminal outcome. Sourced from
        bulk_upload_job_files (covers every file, including CANCELLED ones
        that never get a celery_task_log row), enriched by a batched
        celery_task_log lookup for output_summary/error_message/
        completed_at — not the reverse, since a purely celery_task_log-
        sourced query would silently omit every SKIPPED (cancelled) file.
        Returns (page_entries, total_terminal_count).
        """
        job = self.bulk_upload_job_repo.get_by_id(job_id)
        if job is None:
            raise BulkUploadJobNotFoundException("Bulk upload job not found.")

        files = self.bulk_upload_job_file_repo.get_by_job_id(job_id)
        terminal_files = [f for f in files if f.status in _TERMINAL_FILE_STATUSES]

        task_ids = [f.task_id for f in terminal_files if f.task_id]
        task_logs_by_id = {
            log.task_id: log for log in self.celery_task_log_repo.get_by_task_ids(task_ids)
        }

        entries = [
            self._to_file_log_entry(f, task_logs_by_id.get(f.task_id), job)
            for f in terminal_files
        ]
        entries.sort(key=lambda entry: entry.timestamp, reverse=True)

        total = len(entries)
        return entries[offset:offset + limit], total

    def _to_file_log_entry(
        self,
        job_file: BulkUploadJobFile,
        task_log: CeleryTaskLog | None,
        job: BulkUploadJob,
    ) -> BulkUploadFileLogEntry:
        if job_file.status == BulkUploadFileStatus.CANCELLED:
            result = BulkUploadFileLogResult.SKIPPED
            reason = _SKIPPED_FILE_REASON
        elif job_file.status == BulkUploadFileStatus.FAILED:
            result = BulkUploadFileLogResult.FAILED
            reason = self._resolve_failure_reason(task_log)
        elif self._is_duplicate_file_outcome(task_log):
            result = BulkUploadFileLogResult.DUPLICATE
            reason = None
        else:
            result = BulkUploadFileLogResult.SUCCESS
            reason = None

        return BulkUploadFileLogEntry(
            filename=job_file.original_filename,
            result=result,
            reason=reason,
            timestamp=self._resolve_file_log_timestamp(job_file, task_log, job),
        )

    @staticmethod
    def _is_duplicate_file_outcome(task_log: CeleryTaskLog | None) -> bool:
        """
        Encapsulates the only signal that distinguishes a genuine success
        from an auto-skipped exact-duplicate - both share
        BulkUploadFileStatus.PROCESSED (see the module-level marker
        constant's own comment for why).
        """
        return (
            task_log is not None
            and task_log.output_summary is not None
            and _DUPLICATE_FILE_OUTPUT_SUMMARY_MARKER in task_log.output_summary
        )

    @staticmethod
    def _resolve_failure_reason(task_log: CeleryTaskLog | None) -> str:
        """A FAILED entry must always carry a meaningful reason, even when no task log or error_message exists."""
        if task_log is not None and task_log.error_message:
            return task_log.error_message
        return _DEFAULT_FAILURE_REASON

    @staticmethod
    def _resolve_file_log_timestamp(
        job_file: BulkUploadJobFile,
        task_log: CeleryTaskLog | None,
        job: BulkUploadJob,
    ) -> datetime:
        """
        Prefers the task log's own completed_at (per-file, genuinely
        distinct) over job_file.created_at (extraction-time, identical for
        every file in the job - never useful for "most recent first"
        ordering). CANCELLED files have no per-file resolution timestamp
        at all (no updated_at column on bulk_upload_job_files) - the job's
        own completed_at (set when the job itself was cancelled) is the
        most accurate available signal, falling back to job.created_at if
        even that is somehow absent.
        """
        if task_log is not None and task_log.completed_at is not None:
            return task_log.completed_at
        if job_file.status == BulkUploadFileStatus.CANCELLED:
            return job.completed_at or job.created_at
        return job_file.created_at

    def get_job_detail(self, job_id: UUID) -> tuple[BulkUploadJob, list, dict[str, int]]:
        """
        Phase B8: one job's full detail plus its per-file breakdown, plus a
        {task_id: retry_count} map so the route can surface each file's
        retry-attempt count — one batched celery_task_log query for the
        whole job rather than one query per file.
        """
        job = self.bulk_upload_job_repo.get_by_id(job_id)
        if job is None:
            raise BulkUploadJobNotFoundException("Bulk upload job not found.")

        files = self.bulk_upload_job_file_repo.get_by_job_id(job_id)

        task_ids = [f.task_id for f in files if f.task_id]
        task_logs = self.celery_task_log_repo.get_by_task_ids(task_ids)
        retry_counts = {log.task_id: log.retry_count for log in task_logs}

        return job, files, retry_counts

    def list_history(
        self,
        campaign_id: UUID,
        page: int,
        size: int,
    ) -> tuple[list[BulkUploadJob], int]:
        """Phase B8: paginated bulk-upload history for one campaign."""
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException("Campaign not found.", 404)

        total = self.bulk_upload_job_repo.count_by_campaign(campaign_id)
        offset = (page - 1) * size
        items = self.bulk_upload_job_repo.list_by_campaign(campaign_id, offset=offset, limit=size)
        return items, total

    def export_history(
        self,
        campaign_id: UUID,
        actor_id: str,
        actor_role: str | None = None,
    ):
        """Phase B8: unpaginated Excel export of a campaign's bulk-upload history."""
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException("Campaign not found.", 404)

        records = self.bulk_upload_job_repo.get_all_by_campaign(campaign_id)
        excel_file = ExcelExport.export_bulk_upload_history(records)

        try:
            self.audit_service.log(
                actor_id=actor_id,
                actor_role=actor_role,
                action_type=ActionType.BULK_UPLOAD_HISTORY_EXPORTED,
                entity_type=EntityType.BULK_UPLOAD_JOB,
                entity_id=self.EXPORT_AUDIT_ENTITY_ID,
                campaign_id=campaign_id,
                details={"total_exported_records": len(records)},
            )
            self.bulk_upload_job_repo.commit()
        except Exception:
            self.bulk_upload_job_repo.rollback()
            raise

        return excel_file
