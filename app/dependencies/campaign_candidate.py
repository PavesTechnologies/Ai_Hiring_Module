from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.encryption_service import EncryptionService
from app.core.storage_service import StorageService
from app.db.session import get_db
from app.dependencies.storage import get_storage_service

from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.allowed_transition_repository import AllowedTransitionRepository
from app.repositories.campaign_candidate_ai_evaluation_repository import (
    CampaignCandidateAIEvaluationRepository,
)
from app.repositories.campaign_candidate_repository import (
    CampaignCandidateRepository,
)
from app.repositories.audit_repository import AuditRepository
from app.repositories.candidate_repository import CandidateRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.encryption_key_repository import EncryptionKeyRepository
from app.repositories.resume_repository import ResumeRepository
from app.repositories.skill_repository import SkillRepository

from app.services.audit_service import AuditService
from app.services.campaign.campaign_candidate_service import (
    CampaignCandidateService,
)
from app.services.campaign.pipeline_transition_service import PipelineTransitionService
from app.services.campaign.stage_transition_service import StageTransitionService
from app.services.celery_task_log_service import CeleryTaskLogService
from app.services.resume.file_validation_service import FileValidationService


def get_campaign_repository(
    db: Session = Depends(get_db),
) -> CampaignRepository:
    return CampaignRepository(db)


def get_campaign_candidate_repository(
    db: Session = Depends(get_db),
) -> CampaignCandidateRepository:
    return CampaignCandidateRepository(db)


def get_audit_repository(
    db: Session = Depends(get_db),
) -> AuditRepository:
    return AuditRepository(db)


def get_audit_service(
    repository: AuditRepository = Depends(get_audit_repository),
) -> AuditService:
    return AuditService(repository)


# Defined locally (not imported from app.dependencies.resume) because that
# module already imports get_audit_service/get_campaign_repository from
# this one - importing back from it here would be circular.
def get_encryption_key_repository(
    db: Session = Depends(get_db),
) -> EncryptionKeyRepository:
    return EncryptionKeyRepository(db)


def get_encryption_service(
    repository: EncryptionKeyRepository = Depends(get_encryption_key_repository),
) -> EncryptionService:
    return EncryptionService(repository)


def get_candidate_repository(
    db: Session = Depends(get_db),
) -> CandidateRepository:
    return CandidateRepository(db)


def get_resume_repository(
    db: Session = Depends(get_db),
) -> ResumeRepository:
    return ResumeRepository(db)


def get_campaign_candidate_ai_evaluation_repository(
    db: Session = Depends(get_db),
) -> CampaignCandidateAIEvaluationRepository:
    return CampaignCandidateAIEvaluationRepository(db)


def get_allowed_transition_repository(
    db: Session = Depends(get_db),
) -> AllowedTransitionRepository:
    return AllowedTransitionRepository(db)


def get_pipeline_transition_service(
    allowed_transition_repo: AllowedTransitionRepository = Depends(get_allowed_transition_repository),
    campaign_candidate_repo: CampaignCandidateRepository = Depends(get_campaign_candidate_repository),
    audit_service: AuditService = Depends(get_audit_service),
) -> PipelineTransitionService:
    return PipelineTransitionService(
        allowed_transition_repo=allowed_transition_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        audit_service=audit_service,
    )


def get_stage_transition_service(
    allowed_transition_repo: AllowedTransitionRepository = Depends(
        get_allowed_transition_repository
    ),
    campaign_candidate_repo: CampaignCandidateRepository = Depends(
        get_campaign_candidate_repository
    ),
) -> StageTransitionService:
    return StageTransitionService(allowed_transition_repo, campaign_candidate_repo)


def get_config_repository(
    db: Session = Depends(get_db),
) -> ConfigRepository:
    return ConfigRepository(db)


# Epic 3 (M05-E03) Phase C5 — defined locally (not imported from
# app.dependencies.resume) for the exact same reason get_encryption_service
# above is: that module imports several factories from this one, so
# importing back from it here would be circular.
def get_file_validation_service(
    config_repo: ConfigRepository = Depends(get_config_repository),
) -> FileValidationService:
    return FileValidationService(config_repo)


def get_celery_task_log_repository(
    db: Session = Depends(get_db),
) -> CeleryTaskLogRepository:
    return CeleryTaskLogRepository(db)


def get_celery_task_log_service(
    repository: CeleryTaskLogRepository = Depends(get_celery_task_log_repository),
) -> CeleryTaskLogService:
    return CeleryTaskLogService(repository)


def get_skill_repository(
    db: Session = Depends(get_db),
) -> SkillRepository:
    return SkillRepository(db)


def get_campaign_candidate_service(
    campaign_repo: CampaignRepository = Depends(
        get_campaign_repository
    ),
    campaign_candidate_repo: CampaignCandidateRepository = Depends(
        get_campaign_candidate_repository
    ),
    audit_service: AuditService = Depends(
        get_audit_service
    ),
    encryption_service: EncryptionService = Depends(
        get_encryption_service
    ),
    candidate_repo: CandidateRepository = Depends(
        get_candidate_repository
    ),
    resume_repo: ResumeRepository = Depends(
        get_resume_repository
    ),
    ai_evaluation_repo: CampaignCandidateAIEvaluationRepository = Depends(
        get_campaign_candidate_ai_evaluation_repository
    ),
    stage_transition_service: StageTransitionService = Depends(
        get_stage_transition_service
    ),
    config_repo: ConfigRepository = Depends(
        get_config_repository
    ),
    celery_task_log_service: CeleryTaskLogService = Depends(
        get_celery_task_log_service
    ),
    skill_repo: SkillRepository = Depends(
        get_skill_repository
    ),
    allowed_transition_repo: AllowedTransitionRepository = Depends(
        get_allowed_transition_repository
    ),
    pipeline_transition_service: PipelineTransitionService = Depends(
        get_pipeline_transition_service
    ),
    file_validation_service: FileValidationService = Depends(
        get_file_validation_service
    ),
    storage_service: StorageService = Depends(
        get_storage_service
    ),
) -> CampaignCandidateService:

    return CampaignCandidateService(
        campaign_repo=campaign_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        audit_service=audit_service,
        encryption_service=encryption_service,
        candidate_repo=candidate_repo,
        resume_repo=resume_repo,
        ai_evaluation_repo=ai_evaluation_repo,
        stage_transition_service=stage_transition_service,
        config_repo=config_repo,
        celery_task_log_service=celery_task_log_service,
        skill_repo=skill_repo,
        allowed_transition_repo=allowed_transition_repo,
        pipeline_transition_service=pipeline_transition_service,
        file_validation_service=file_validation_service,
        storage_service=storage_service,
    )