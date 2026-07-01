from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.repositories.jd_repository import JDRepository
from app.services.jd.jd_service import JDService
from app.services.jd.hash_service import HashService

from app.repositories.audit_repository import AuditRepository
from app.services.audit_service import AuditService


def get_jd_repository(
    db: Session = Depends(get_db),
)-> JDRepository:
    return JDRepository(db)

def get_hash_service() -> HashService:
    return HashService()


def get_audit_repository(
    db: Session = Depends(get_db),
)-> AuditRepository:
    return AuditRepository(db)


def get_audit_service(
    repository: AuditRepository = Depends(get_audit_repository),
)-> AuditService:
    return AuditService(repository=repository)


def get_jd_service(
    repository: JDRepository = Depends(get_jd_repository),
    hash_service: HashService = Depends(get_hash_service),
    audit_service: AuditService = Depends(get_audit_service),
)-> JDService:
    return JDService(
        repository=repository,
        hash_service=hash_service,
        audit_service=audit_service,
    )
    