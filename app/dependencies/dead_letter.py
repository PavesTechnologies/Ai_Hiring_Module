from fastapi import Depends

from app.dependencies.jd import get_celery_task_log_repository, get_checkpoint_repository, get_document_processing_repository
from app.dependencies.resume import get_dead_letter_queue_repository, get_stage_failure_log_repository
from app.dependencies.storage import get_storage_service
from app.core.storage_service import StorageService
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.checkpoint_repository import CheckpointRepository
from app.repositories.dead_letter_queue_repository import DeadLetterQueueRepository
from app.repositories.document_processing_repository import DocumentProcessingRepository
from app.repositories.stage_failure_log_repository import StageFailureLogRepository
from app.services.document_processing.dead_letter_cleanup_service import DeadLetterCleanupService


def get_dead_letter_cleanup_service(
    dead_letter_queue_repo: DeadLetterQueueRepository = Depends(get_dead_letter_queue_repository),
    celery_task_log_repo: CeleryTaskLogRepository = Depends(get_celery_task_log_repository),
    checkpoint_repo: CheckpointRepository = Depends(get_checkpoint_repository),
    stage_failure_log_repo: StageFailureLogRepository = Depends(get_stage_failure_log_repository),
    document_processing_repo: DocumentProcessingRepository = Depends(get_document_processing_repository),
    storage_service: StorageService = Depends(get_storage_service),
) -> DeadLetterCleanupService:
    return DeadLetterCleanupService(
        dead_letter_queue_repo=dead_letter_queue_repo,
        celery_task_log_repo=celery_task_log_repo,
        checkpoint_repo=checkpoint_repo,
        stage_failure_log_repo=stage_failure_log_repo,
        document_processing_repo=document_processing_repo,
        storage_service=storage_service,
    )
