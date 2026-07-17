from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.repositories.skill_repository import SkillRepository
from app.services.skills.unknown_skill_service import UnknownSkillService


def get_skill_repository(
    db: Session = Depends(get_db),
) -> SkillRepository:
    return SkillRepository(db)


def get_unknown_skill_service(
    repository: SkillRepository = Depends(get_skill_repository),
) -> UnknownSkillService:
    return UnknownSkillService(repository=repository)
