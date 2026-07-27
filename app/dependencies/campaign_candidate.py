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
from app.repositories.encryption_key_repository import EncryptionKeyRepository

from app.services.audit_service import AuditService
from app.services.campaign.campaign_candidate_service import (
    CampaignCandidateService,
)
from app.services.campaign.pipeline_transition_service import PipelineTransitionService


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
) -> CampaignCandidateService:

    return CampaignCandidateService(
        campaign_repo=campaign_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        audit_service=audit_service,
        encryption_service=encryption_service,
    )