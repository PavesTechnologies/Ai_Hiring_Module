from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.encryption_service import EncryptionService
from app.db.session import get_db
# Defined locally rather than imported from app.dependencies.campaign_candidate
# to avoid pulling in that module's much larger dependency graph for a
# single leaf factory.
from app.repositories.encryption_key_repository import EncryptionKeyRepository
from app.repositories.dashboard_repository import DashboardRepository
from app.services.dashboard.dashboard_service import DashboardService


def get_dashboard_repository(
    db: Session = Depends(get_db),
) -> DashboardRepository:
    return DashboardRepository(db)


def get_encryption_key_repository_for_dashboard(
    db: Session = Depends(get_db),
) -> EncryptionKeyRepository:
    return EncryptionKeyRepository(db)


def get_encryption_service_for_dashboard(
    repository: EncryptionKeyRepository = Depends(get_encryption_key_repository_for_dashboard),
) -> EncryptionService:
    return EncryptionService(repository)


def get_dashboard_service(
    repo: DashboardRepository = Depends(get_dashboard_repository),
    encryption_service: EncryptionService = Depends(get_encryption_service_for_dashboard),
) -> DashboardService:
    return DashboardService(repo=repo, encryption_service=encryption_service)
