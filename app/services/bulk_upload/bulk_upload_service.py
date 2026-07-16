from uuid import UUID, uuid4

from app.core.storage_service import StorageService
from app.exceptions.campaign_exceptions import CampaignException
from app.models.async_tasks import BulkUploadJob, BulkUploadStatus
from app.models.campaigns import CampaignStatus, HiringCampaign
from app.repositories.bulk_upload_job_repository import BulkUploadJobRepository
from app.repositories.CampaignRepository import CampaignRepository
from app.services.bulk_upload.zip_validation_service import ZipValidationService
from app.tasks.bulk_upload_tasks import extract_bulk_upload_zip


class BulkUploadService:
    """
    Validates the ZIP, stores it, creates the bulk_upload_jobs record at
    status=PENDING, and enqueues the BULK_EXTRACT task (Phase B3) which
    unpacks the archive asynchronously. No per-file parsing happens here
    or in BULK_EXTRACT itself — that's Phase B4.
    """

    BULK_UPLOAD_STORAGE_BUCKET = "airs_resumes"

    def __init__(
        self,
        bulk_upload_job_repo: BulkUploadJobRepository,
        zip_validation_service: ZipValidationService,
        storage_service: StorageService,
        campaign_repo: CampaignRepository,
    ):
        self.bulk_upload_job_repo = bulk_upload_job_repo
        self.zip_validation_service = zip_validation_service
        self.storage_service = storage_service
        self.campaign_repo = campaign_repo

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
