from fastapi import Depends

from app.dependencies.bulk_upload import get_bulk_upload_job_file_repository, get_bulk_upload_job_repository
from app.dependencies.campaign import get_campaign_repository
from app.dependencies.jd import get_celery_task_log_repository, get_document_processing_repository
from app.dependencies.resume import (
    get_circuit_breaker_repository,
    get_resume_repository,
    get_stage_failure_log_repository,
)
from app.repositories.bulk_upload_job_file_repository import BulkUploadJobFileRepository
from app.repositories.bulk_upload_job_repository import BulkUploadJobRepository
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.circuit_breaker_repository import CircuitBreakerRepository
from app.repositories.document_processing_repository import DocumentProcessingRepository
from app.repositories.resume_repository import ResumeRepository
from app.repositories.stage_failure_log_repository import StageFailureLogRepository
from app.services.ops_monitoring_service import OpsMonitoringService


def get_ops_monitoring_service(
    celery_task_log_repository: CeleryTaskLogRepository = Depends(get_celery_task_log_repository),
    bulk_upload_job_file_repository: BulkUploadJobFileRepository = Depends(get_bulk_upload_job_file_repository),
    stage_repository: DocumentProcessingRepository = Depends(get_document_processing_repository),
    stage_failure_log_repository: StageFailureLogRepository = Depends(get_stage_failure_log_repository),
    resume_repository: ResumeRepository = Depends(get_resume_repository),
    bulk_upload_job_repository: BulkUploadJobRepository = Depends(get_bulk_upload_job_repository),
    campaign_repository: CampaignRepository = Depends(get_campaign_repository),
    circuit_breaker_repository: CircuitBreakerRepository = Depends(get_circuit_breaker_repository),
) -> OpsMonitoringService:
    return OpsMonitoringService(
        celery_task_log_repository=celery_task_log_repository,
        bulk_upload_job_file_repository=bulk_upload_job_file_repository,
        stage_repository=stage_repository,
        stage_failure_log_repository=stage_failure_log_repository,
        resume_repository=resume_repository,
        bulk_upload_job_repository=bulk_upload_job_repository,
        campaign_repository=campaign_repository,
        circuit_breaker_repository=circuit_breaker_repository,
    )
