from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies.jd import get_audit_service
from app.repositories.skill_repository import SkillRepository
from app.services.audit_service import AuditService
from app.services.skills.skill_curation_service import SkillCurationService


def get_skill_repository(
    db: Session = Depends(get_db),
) -> SkillRepository:
    return SkillRepository(db)


def get_skill_curation_service(
    skill_repository: SkillRepository = Depends(get_skill_repository),
    audit_service: AuditService = Depends(get_audit_service),
) -> SkillCurationService:
    return SkillCurationService(
        skill_repository=skill_repository,
        audit_service=audit_service,
    )
