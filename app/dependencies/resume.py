from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.encryption_service import EncryptionService
from app.core.storage_service import StorageService
from app.db.session import get_db
from app.dependencies.campaign import get_config_repository
from app.dependencies.campaign_candidate import (
    get_audit_service,
    get_campaign_candidate_service,
    get_campaign_repository,
)
from app.dependencies.jd import (
    get_celery_task_log_repository,
    get_document_processing_repository,
)
from app.dependencies.storage import get_storage_service
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.candidate_repository import CandidateRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.circuit_breaker_repository import CircuitBreakerRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.consent_repository import ConsentRepository
from app.repositories.document_processing_repository import DocumentProcessingRepository
from app.repositories.encryption_key_repository import EncryptionKeyRepository
from app.repositories.resume_repository import ResumeRepository
from app.services.audit_service import AuditService
from app.services.campaign.campaign_candidate_service import CampaignCandidateService
from app.services.compliance.consent_service import ConsentService
from app.services.resume.candidate_service import CandidateService
from app.services.resume.file_validation_service import FileValidationService
from app.services.resume.resume_intake_service import ResumeIntakeService
from app.services.resume.resume_processing_status_service import ResumeProcessingStatusService
from app.services.resume.resume_service import ResumeService


def get_encryption_key_repository(
    db: Session = Depends(get_db),
) -> EncryptionKeyRepository:
    return EncryptionKeyRepository(db)


def get_encryption_service(
    repository: EncryptionKeyRepository = Depends(get_encryption_key_repository),
) -> EncryptionService:
    return EncryptionService(repository)


def get_consent_repository(
    db: Session = Depends(get_db),
) -> ConsentRepository:
    return ConsentRepository(db)


def get_consent_service(
    consent_repo: ConsentRepository = Depends(get_consent_repository),
    config_repo: ConfigRepository = Depends(get_config_repository),
) -> ConsentService:
    return ConsentService(consent_repo, config_repo)


def get_candidate_repository(
    db: Session = Depends(get_db),
) -> CandidateRepository:
    return CandidateRepository(db)


def get_candidate_service(
    candidate_repo: CandidateRepository = Depends(get_candidate_repository),
    encryption_service: EncryptionService = Depends(get_encryption_service),
    consent_service: ConsentService = Depends(get_consent_service),
) -> CandidateService:
    return CandidateService(candidate_repo, encryption_service, consent_service)


def get_resume_repository(
    db: Session = Depends(get_db),
) -> ResumeRepository:
    return ResumeRepository(db)


def get_file_validation_service(
    config_repo: ConfigRepository = Depends(get_config_repository),
) -> FileValidationService:
    return FileValidationService(config_repo)


def get_circuit_breaker_repository(
    db: Session = Depends(get_db),
) -> CircuitBreakerRepository:
    return CircuitBreakerRepository(db)


def get_resume_service(
    resume_repo: ResumeRepository = Depends(get_resume_repository),
    candidate_service: CandidateService = Depends(get_candidate_service),
    file_validation_service: FileValidationService = Depends(get_file_validation_service),
    storage_service: StorageService = Depends(get_storage_service),
    circuit_breaker_repo: CircuitBreakerRepository = Depends(get_circuit_breaker_repository),
    audit_service: AuditService = Depends(get_audit_service),
) -> ResumeService:
    return ResumeService(
        resume_repo=resume_repo,
        candidate_service=candidate_service,
        file_validation_service=file_validation_service,
        storage_service=storage_service,
        circuit_breaker_repo=circuit_breaker_repo,
        audit_service=audit_service,
    )


def get_resume_intake_service(
    resume_service: ResumeService = Depends(get_resume_service),
    campaign_candidate_service: CampaignCandidateService = Depends(get_campaign_candidate_service),
    campaign_repo: CampaignRepository = Depends(get_campaign_repository),
    audit_service: AuditService = Depends(get_audit_service),
) -> ResumeIntakeService:
    return ResumeIntakeService(
        resume_service=resume_service,
        campaign_candidate_service=campaign_candidate_service,
        campaign_repo=campaign_repo,
        audit_service=audit_service,
    )


def get_resume_processing_status_service(
    task_log_repository: CeleryTaskLogRepository = Depends(get_celery_task_log_repository),
    stage_repository: DocumentProcessingRepository = Depends(get_document_processing_repository),
) -> ResumeProcessingStatusService:
    return ResumeProcessingStatusService(
        task_log_repository=task_log_repository,
        stage_repository=stage_repository,
    )
