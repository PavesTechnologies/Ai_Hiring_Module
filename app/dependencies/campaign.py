from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.repositories.audit_repository import AuditRepository
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.jd_repository import JDRepository
from app.repositories.prompt_template_repository import PromptTemplateRepository
from app.services.audit_service import AuditService
from app.services.campaign.campaign_service import CampaignService
from app.services.campaign.campaign_scheduler_service import CampaignSchedulerService
from app.repositories.campaign_weight_preset_repository import (
    CampaignWeightPresetRepository,
)

# Epic 4 (M05-E04) Phase D7 — imported directly (not via
# app.dependencies.resume/app.dependencies.bulk_upload) since both of
# those modules already import get_config_repository from this one;
# importing back from either here would be circular. ResumeRepository
# and BulkUploadJobRepository take only a Session, so a local factory is
# trivial, mirroring the same pattern already used elsewhere for exactly
# this reason (e.g. app/dependencies/campaign_candidate.py's
# get_encryption_service).
from app.repositories.bulk_upload_job_repository import BulkUploadJobRepository
from app.repositories.resume_repository import ResumeRepository
from app.services.upload_history.upload_history_service import UploadHistoryService
from app.dependencies.cache import get_cache_service
from app.services.cache_service import CacheService

def get_campaign_repository(
    db: Session = Depends(get_db),
) -> CampaignRepository:
    return CampaignRepository(db)

def get_campaign_weight_preset_repository(
    db: Session = Depends(get_db),
) -> CampaignWeightPresetRepository:
    return CampaignWeightPresetRepository(db)

def get_jd_repository(
    db: Session = Depends(get_db),
) -> JDRepository:
    return JDRepository(db)

def get_prompt_template_repository(
    db: Session = Depends(get_db),
) -> PromptTemplateRepository:
    return PromptTemplateRepository(db)

def get_config_repository(
    db: Session = Depends(get_db),
) -> ConfigRepository:
    return ConfigRepository(db)

def get_audit_repository(
    db: Session = Depends(get_db),
) -> AuditRepository:
    return AuditRepository(db)


def get_audit_service(
    repository: AuditRepository = Depends(get_audit_repository),
    campaign_repo: CampaignRepository = Depends(get_campaign_repository),
) -> AuditService:
    return AuditService(repository=repository, campaign_repo=campaign_repo)


def get_campaign_service(
    campaign_repo: CampaignRepository = Depends(get_campaign_repository),
    jd_repo: JDRepository = Depends(get_jd_repository),
    audit_service: AuditService = Depends(get_audit_service),
    config_repo: ConfigRepository = Depends(get_config_repository),
    preset_repo: CampaignWeightPresetRepository = Depends(get_campaign_weight_preset_repository),
    db: Session = Depends(get_db),
    prompt_template_repo: PromptTemplateRepository = Depends(get_prompt_template_repository),
    cache_service: CacheService = Depends(get_cache_service),
) -> CampaignService:
    return CampaignService(
        campaign_repo=campaign_repo,
        jd_repo=jd_repo,
        audit_service=audit_service,
        config_repo=config_repo,
        preset_repo=preset_repo,
        db=db,
        prompt_template_repo=prompt_template_repo,
        cache_service=cache_service,
    )

def get_resume_repository_for_upload_history(
    db: Session = Depends(get_db),
) -> ResumeRepository:
    return ResumeRepository(db)


def get_bulk_upload_job_repository_for_upload_history(
    db: Session = Depends(get_db),
) -> BulkUploadJobRepository:
    return BulkUploadJobRepository(db)


def get_upload_history_service(
    campaign_repo: CampaignRepository = Depends(get_campaign_repository),
    resume_repo: ResumeRepository = Depends(get_resume_repository_for_upload_history),
    bulk_upload_job_repo: BulkUploadJobRepository = Depends(get_bulk_upload_job_repository_for_upload_history),
) -> UploadHistoryService:
    return UploadHistoryService(
        campaign_repo=campaign_repo,
        resume_repo=resume_repo,
        bulk_upload_job_repo=bulk_upload_job_repo,
    )


def get_campaign_scheduler_service(
    campaign_repo: CampaignRepository = Depends(get_campaign_repository),
    audit_service: AuditService = Depends(get_audit_service),
    config_repo: ConfigRepository = Depends(get_config_repository),
) -> CampaignSchedulerService:
    return CampaignSchedulerService(
        campaign_repo=campaign_repo,
        audit_service=audit_service,
        config_repo=config_repo,
    )