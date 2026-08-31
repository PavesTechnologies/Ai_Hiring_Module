from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.encryption_service import EncryptionService
from app.db.session import get_db
from app.dependencies.cache import get_cache_service
from app.repositories.candidate_filter_repository import CandidateFilterRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.dashboard_repository import DashboardRepository
# Defined locally rather than imported from app.dependencies.campaign_candidate
# to avoid pulling in that module's much larger dependency graph for a
# single leaf factory.
from app.repositories.encryption_key_repository import EncryptionKeyRepository
from app.repositories.skill_search_repository import SkillSearchRepository
from app.services.cache_service import CacheService
from app.services.dashboard.candidate_search_service import CandidateSearchService
from app.services.dashboard.dashboard_service import DashboardService


def get_dashboard_repository(db: Session = Depends(get_db)) -> DashboardRepository:
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
    dashboard_repo: DashboardRepository = Depends(get_dashboard_repository),
    encryption_service: EncryptionService = Depends(get_encryption_service_for_dashboard),
    db: Session = Depends(get_db),
    cache_service: CacheService = Depends(get_cache_service),
) -> DashboardService:
    return DashboardService(
        dashboard_repo=dashboard_repo,
        config_repo=ConfigRepository(db),
        encryption_service=encryption_service,
        cache_service=cache_service,
    )


def get_candidate_search_service(db: Session = Depends(get_db)) -> CandidateSearchService:
    return CandidateSearchService(
        skill_search_repo=SkillSearchRepository(db),
        candidate_filter_repo=CandidateFilterRepository(db),
    )
