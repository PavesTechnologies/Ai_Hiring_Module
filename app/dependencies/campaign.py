from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.repositories.audit_repository import AuditRepository
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.jd_repository import JDRepository
from app.services.audit_service import AuditService
from app.services.campaign.campaign_service import CampaignService
from app.services.campaign.campaign_scheduler_service import CampaignSchedulerService
from app.repositories.campaign_weight_preset_repository import (
    CampaignWeightPresetRepository,
)

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
) -> AuditService:
    return AuditService(repository=repository)


def get_campaign_service(
    campaign_repo: CampaignRepository = Depends(get_campaign_repository),
    jd_repo: JDRepository = Depends(get_jd_repository),
    audit_service: AuditService = Depends(get_audit_service),
    config_repo: ConfigRepository = Depends(get_config_repository),
    preset_repo: CampaignWeightPresetRepository = Depends(get_campaign_weight_preset_repository),
    db: Session = Depends(get_db),
) -> CampaignService:
    return CampaignService(
        campaign_repo=campaign_repo,
        jd_repo=jd_repo,
        audit_service=audit_service,
        config_repo=config_repo,
        preset_repo=preset_repo,
        db=db,
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