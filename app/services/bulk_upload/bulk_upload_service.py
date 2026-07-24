from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.core.storage_service import StorageService
from app.enums.constants import ActionType, EntityType
from app.exceptions.bulk_upload_exceptions import (
    BulkUploadJobNotCancellableException,
    BulkUploadJobNotFoundException,
)
from app.exceptions.campaign_exceptions import CampaignException
from app.models.async_tasks import BulkUploadJob, BulkUploadStatus
from app.models.campaigns import CampaignStatus, HiringCampaign
from app.repositories.bulk_upload_job_file_repository import BulkUploadJobFileRepository
from app.repositories.bulk_upload_job_repository import BulkUploadJobRepository
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.services.audit_service import AuditService
from app.services.bulk_upload.zip_validation_service import ZipValidationService
from app.tasks.bulk_upload_tasks import extract_bulk_upload_zip
from app.utils.excel_export import ExcelExport

_TERMINAL_JOB_STATUSES = (
    BulkUploadStatus.COMPLETED,
    BulkUploadStatus.PARTIAL_FAILURE,
    BulkUploadStatus.FAILED,
    BulkUploadStatus.CANCELLED,
)


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
    ):
        self.bulk_upload_job_repo = bulk_upload_job_repo
        self.bulk_upload_job_file_repo = bulk_upload_job_file_repo
        self.zip_validation_service = zip_validation_service
        self.storage_service = storage_service
        self.campaign_repo = campaign_repo
        self.audit_service = audit_service
        self.celery_task_log_repo = celery_task_log_repo

    def upload_zip(
        self,
        campaign_id: UUID,
        file_bytes: bytes,
        filename: str,
        uploaded_by: str,
        consent_confirmed: bool,
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
                "Campaign is closed. Resume uploads are not allowed.", 409,
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
