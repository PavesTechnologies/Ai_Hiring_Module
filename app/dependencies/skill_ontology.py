from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.repositories.audit_repository import AuditRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.skill_ontology_repository import SkillOntologyRepository
from app.repositories.skill_repository import SkillRepository
from app.services.audit_service import AuditService
from app.services.embedding_queue_service import EmbeddingQueueService
from app.services.skills.SkillOntologyService import SkillOntologyService


def get_skill_ontology_repository(
    db: Session = Depends(get_db),
) -> SkillOntologyRepository:
    return SkillOntologyRepository(db)


def get_skill_repository(
    db: Session = Depends(get_db),
) -> SkillRepository:
    return SkillRepository(db)


def get_config_repository(
    db: Session = Depends(get_db),
) -> ConfigRepository:
    return ConfigRepository(db)


def get_audit_repository(
    db: Session = Depends(get_db),
) -> AuditRepository:
    return AuditRepository(db)


def get_audit_service(
    repository: AuditRepository = Depends(get_audit_repository),
) -> AuditService:
    return AuditService(repository=repository)


def get_celery_task_log_repository(
    db: Session = Depends(get_db),
) -> CeleryTaskLogRepository:
    return CeleryTaskLogRepository(db)


def get_embedding_queue_service() -> EmbeddingQueueService:
    return EmbeddingQueueService()


def get_skill_ontology_service(
    repository: SkillOntologyRepository = Depends(get_skill_ontology_repository),
    db: Session = Depends(get_db),
    skill_repository: SkillRepository = Depends(get_skill_repository),
    config_repository: ConfigRepository = Depends(get_config_repository),
    audit_service: AuditService = Depends(get_audit_service),
    celery_task_log_repository: CeleryTaskLogRepository = Depends(get_celery_task_log_repository),
    embedding_queue_service: EmbeddingQueueService = Depends(get_embedding_queue_service),
) -> SkillOntologyService:
    return SkillOntologyService(
        repository=repository,
        db=db,
        skill_repository=skill_repository,
        config_repository=config_repository,
        audit_service=audit_service,
        celery_task_log_repository=celery_task_log_repository,
        embedding_queue_service=embedding_queue_service,
    )
