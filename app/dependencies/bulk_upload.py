from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.encryption_service import EncryptionService
from app.core.storage_service import StorageService
from app.db.session import get_db
from app.dependencies.campaign import get_config_repository
from app.dependencies.campaign_candidate import get_audit_service, get_campaign_repository
from app.dependencies.jd import get_celery_task_log_repository, get_document_processing_repository
from app.dependencies.resume import (
    get_candidate_repository,
    get_dead_letter_queue_repository,
    get_encryption_service,
    get_resume_repository,
    get_stage_failure_log_repository,
)
from app.dependencies.storage import get_storage_service
from app.repositories.bulk_upload_job_file_repository import BulkUploadJobFileRepository
from app.repositories.bulk_upload_job_repository import BulkUploadJobRepository
from app.repositories.candidate_repository import CandidateRepository
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.dead_letter_queue_repository import DeadLetterQueueRepository
from app.repositories.document_processing_repository import DocumentProcessingRepository
from app.repositories.resume_repository import ResumeRepository
from app.repositories.stage_failure_log_repository import StageFailureLogRepository
from app.services.audit_service import AuditService
from app.services.bulk_upload.bulk_upload_monitoring_service import BulkUploadMonitoringService
from app.services.bulk_upload.bulk_upload_service import BulkUploadService
from app.services.bulk_upload.zip_validation_service import ZipValidationService


def get_bulk_upload_job_repository(
    db: Session = Depends(get_db),
) -> BulkUploadJobRepository:
    return BulkUploadJobRepository(db)


def get_bulk_upload_job_file_repository(
    db: Session = Depends(get_db),
) -> BulkUploadJobFileRepository:
    return BulkUploadJobFileRepository(db)


def get_zip_validation_service(
    config_repo: ConfigRepository = Depends(get_config_repository),
) -> ZipValidationService:
    return ZipValidationService(config_repo)


def get_bulk_upload_service(
    bulk_upload_job_repo: BulkUploadJobRepository = Depends(get_bulk_upload_job_repository),
    bulk_upload_job_file_repo: BulkUploadJobFileRepository = Depends(get_bulk_upload_job_file_repository),
    zip_validation_service: ZipValidationService = Depends(get_zip_validation_service),
    storage_service: StorageService = Depends(get_storage_service),
    campaign_repo: CampaignRepository = Depends(get_campaign_repository),
    audit_service: AuditService = Depends(get_audit_service),
    celery_task_log_repo: CeleryTaskLogRepository = Depends(get_celery_task_log_repository),
) -> BulkUploadService:
    return BulkUploadService(
        bulk_upload_job_repo=bulk_upload_job_repo,
        bulk_upload_job_file_repo=bulk_upload_job_file_repo,
        zip_validation_service=zip_validation_service,
        storage_service=storage_service,
        campaign_repo=campaign_repo,
        audit_service=audit_service,
        celery_task_log_repo=celery_task_log_repo,
    )


def get_bulk_upload_monitoring_service(
    bulk_upload_job_repo: BulkUploadJobRepository = Depends(get_bulk_upload_job_repository),
    bulk_upload_job_file_repo: BulkUploadJobFileRepository = Depends(get_bulk_upload_job_file_repository),
    resume_repository: ResumeRepository = Depends(get_resume_repository),
    candidate_repository: CandidateRepository = Depends(get_candidate_repository),
    encryption_service: EncryptionService = Depends(get_encryption_service),
    task_log_repository: CeleryTaskLogRepository = Depends(get_celery_task_log_repository),
    stage_repository: DocumentProcessingRepository = Depends(get_document_processing_repository),
    stage_failure_log_repository: StageFailureLogRepository = Depends(get_stage_failure_log_repository),
    dead_letter_queue_repository: DeadLetterQueueRepository = Depends(get_dead_letter_queue_repository),
) -> BulkUploadMonitoringService:
    return BulkUploadMonitoringService(
        bulk_upload_job_repo=bulk_upload_job_repo,
        bulk_upload_job_file_repo=bulk_upload_job_file_repo,
        resume_repository=resume_repository,
        candidate_repository=candidate_repository,
        encryption_service=encryption_service,
        task_log_repository=task_log_repository,
        stage_repository=stage_repository,
        stage_failure_log_repository=stage_failure_log_repository,
        dead_letter_queue_repository=dead_letter_queue_repository,
    )
