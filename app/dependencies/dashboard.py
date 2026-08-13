from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.repositories.candidate_filter_repository import CandidateFilterRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.dashboard_repository import DashboardRepository
from app.repositories.skill_search_repository import SkillSearchRepository
from app.services.dashboard.candidate_search_service import CandidateSearchService
from app.services.dashboard.dashboard_service import DashboardService


def get_dashboard_repository(db: Session = Depends(get_db)) -> DashboardRepository:
    return DashboardRepository(db)


def get_dashboard_service(
    dashboard_repo: DashboardRepository = Depends(get_dashboard_repository),
    db: Session = Depends(get_db),
) -> DashboardService:
    return DashboardService(dashboard_repo=dashboard_repo, config_repo=ConfigRepository(db))


def get_candidate_search_service(db: Session = Depends(get_db)) -> CandidateSearchService:
    return CandidateSearchService(
        skill_search_repo=SkillSearchRepository(db),
        candidate_filter_repo=CandidateFilterRepository(db),
    )
