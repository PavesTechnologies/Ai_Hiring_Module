from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.repositories.jd_repository import JDRepository
from app.services.jd.jd_service import JDService
from app.services.jd.hash_service import HashService

from app.repositories.audit_repository import AuditRepository
from app.services.audit_service import AuditService
from app.dependencies.storage import get_storage_service
from app.core.storage_service import StorageService

from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.document_processing_repository import DocumentProcessingRepository
from app.services.document_processing.stage_execution_service import StageExecutionService
from app.services.jd.jd_processing_status_service import JDProcessingStatusService


def get_jd_repository(
    db: Session = Depends(get_db),
)-> JDRepository:
    return JDRepository(db)

def get_hash_service() -> HashService:
    return HashService()


def get_audit_repository(
    db: Session = Depends(get_db),
)-> AuditRepository:
    return AuditRepository(db)


def get_audit_service(
    repository: AuditRepository = Depends(get_audit_repository),
)-> AuditService:
    return AuditService(repository=repository)


def get_jd_service(
    repository: JDRepository = Depends(get_jd_repository),
    hash_service: HashService = Depends(get_hash_service),
    audit_service: AuditService = Depends(get_audit_service),
    storage_service: StorageService = Depends(get_storage_service),
) -> JDService:

    return JDService(
        repository=repository,
        hash_service=hash_service,
        audit_service=audit_service,
        storage_service=storage_service,
    )


def get_document_processing_repository(
    db: Session = Depends(get_db),
) -> DocumentProcessingRepository:
    return DocumentProcessingRepository(db)


def get_stage_tracker(
    repository: DocumentProcessingRepository = Depends(get_document_processing_repository),
) -> StageExecutionService:
    return StageExecutionService(repository)


def get_celery_task_log_repository(
    db: Session = Depends(get_db),
) -> CeleryTaskLogRepository:
    return CeleryTaskLogRepository(db)


def get_jd_processing_status_service(
    task_log_repository: CeleryTaskLogRepository = Depends(get_celery_task_log_repository),
    stage_repository: DocumentProcessingRepository = Depends(get_document_processing_repository),
) -> JDProcessingStatusService:
    return JDProcessingStatusService(
        task_log_repository=task_log_repository,
        stage_repository=stage_repository,
    )
