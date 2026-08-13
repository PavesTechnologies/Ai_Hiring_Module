from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.repositories.candidate_filter_repository import CandidateFilterRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.dashboard_repository import DashboardRepository
from app.repositories.saved_view_repository import SavedViewRepository
from app.repositories.skill_search_repository import SkillSearchRepository
from app.services.dashboard.dashboard_service import DashboardService
from app.services.dashboard.saved_view_service import SavedViewService


def get_dashboard_repository(db: Session = Depends(get_db)) -> DashboardRepository:
    return DashboardRepository(db)


def get_dashboard_service(
    dashboard_repo: DashboardRepository = Depends(get_dashboard_repository),
    db: Session = Depends(get_db),
) -> DashboardService:
    return DashboardService(dashboard_repo=dashboard_repo, config_repo=ConfigRepository(db))


def get_saved_view_service(db: Session = Depends(get_db)) -> SavedViewService:
    return SavedViewService(
        saved_view_repo=SavedViewRepository(db),
        config_repo=ConfigRepository(db),
        skill_search_repo=SkillSearchRepository(db),
        candidate_filter_repo=CandidateFilterRepository(db),
    )
