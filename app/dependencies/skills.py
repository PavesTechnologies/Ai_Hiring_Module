from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.encryption_service import EncryptionService
from app.db.session import get_db
from app.dependencies.jd import get_audit_service
from app.dependencies.skill_ontology import get_embedding_queue_service
from app.repositories.encryption_key_repository import EncryptionKeyRepository
from app.repositories.skill_repository import SkillRepository
from app.services.audit_service import AuditService
from app.services.embedding_queue_service import EmbeddingQueueService
from app.services.skills.skill_curation_service import SkillCurationService


def get_skill_repository(
    db: Session = Depends(get_db),
) -> SkillRepository:
    return SkillRepository(db)


# Defined locally (not imported from app.dependencies.campaign_candidate or
# .resume, which both already define the same pair) — same reasoning as
# campaign_candidate.py's own copy: importing across dependency modules here
# risks circularity for no real benefit.
def get_encryption_key_repository(
    db: Session = Depends(get_db),
) -> EncryptionKeyRepository:
    return EncryptionKeyRepository(db)


def get_encryption_service(
    repository: EncryptionKeyRepository = Depends(get_encryption_key_repository),
) -> EncryptionService:
    return EncryptionService(repository)


def get_skill_curation_service(
    skill_repository: SkillRepository = Depends(get_skill_repository),
    audit_service: AuditService = Depends(get_audit_service),
    embedding_queue_service: EmbeddingQueueService = Depends(get_embedding_queue_service),
    encryption_service: EncryptionService = Depends(get_encryption_service),
) -> SkillCurationService:
    return SkillCurationService(
        skill_repository=skill_repository,
        audit_service=audit_service,
        embedding_queue_service=embedding_queue_service,
        encryption_service=encryption_service,
    )
