from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db

from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.campaign_candidate_repository import (
    CampaignCandidateRepository,
)
from app.repositories.audit_repository import AuditRepository

from app.services.audit_service import AuditService
from app.services.campaign.campaign_candidate_service import (
    CampaignCandidateService,
)


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
) -> CampaignCandidateService:

    return CampaignCandidateService(
        campaign_repo=campaign_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        audit_service=audit_service,
    )