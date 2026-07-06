from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.repositories.audit_repository import AuditRepository
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.jd_repository import JDRepository
from app.services.audit_service import AuditService
from app.services.campaign.campaign_service import CampaignService


def get_campaign_repository(
    db: Session = Depends(get_db),
) -> CampaignRepository:
    return CampaignRepository(db)


def get_jd_repository(
    db: Session = Depends(get_db),
) -> JDRepository:
    return JDRepository(db)


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
    db: Session = Depends(get_db),
) -> CampaignService:
    return CampaignService(
        campaign_repo=campaign_repo,
        jd_repo=jd_repo,
        audit_service=audit_service,
        db=db,
    )
