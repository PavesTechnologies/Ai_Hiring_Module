from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.encryption_service import EncryptionService
from app.db.session import get_db

from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.allowed_transition_repository import AllowedTransitionRepository
from app.repositories.campaign_candidate_repository import (
    CampaignCandidateRepository,
)
from app.repositories.audit_repository import AuditRepository
from app.repositories.candidate_rejection_repository import CandidateRejectionRepository
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
from app.services.campaign.stage_transition_service import StageTransitionService
from app.services.celery_task_log_service import CeleryTaskLogService


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


def get_candidate_rejection_repository(
    db: Session = Depends(get_db),
) -> CandidateRejectionRepository:
    return CandidateRejectionRepository(db)


def get_allowed_transition_repository(
    db: Session = Depends(get_db),
) -> AllowedTransitionRepository:
    return AllowedTransitionRepository(db)


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
    candidate_rejection_repo: CandidateRejectionRepository = Depends(
        get_candidate_rejection_repository
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
) -> CampaignCandidateService:

    return CampaignCandidateService(
        campaign_repo=campaign_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        audit_service=audit_service,
        encryption_service=encryption_service,
        candidate_repo=candidate_repo,
        resume_repo=resume_repo,
        candidate_rejection_repo=candidate_rejection_repo,
        stage_transition_service=stage_transition_service,
        config_repo=config_repo,
        celery_task_log_service=celery_task_log_service,
        skill_repo=skill_repo,
    )