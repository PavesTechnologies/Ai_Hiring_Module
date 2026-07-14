from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.repositories.skill_ontology_repository import SkillOntologyRepository
from app.services.skills.SkillOntologyService import SkillOntologyService


def get_skill_ontology_repository(
    db: Session = Depends(get_db),
) -> SkillOntologyRepository:
    return SkillOntologyRepository(db)


def get_skill_ontology_service(
    repository: SkillOntologyRepository = Depends(get_skill_ontology_repository),
    db: Session = Depends(get_db),
) -> SkillOntologyService:
    return SkillOntologyService(repository=repository, db=db)
