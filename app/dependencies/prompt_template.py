from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.repositories.audit_repository import AuditRepository
from app.repositories.prompt_template_repository import PromptTemplateRepository
from app.services.audit_service import AuditService
from app.services.prompt_template_service import PromptTemplateService


def get_prompt_template_repository(
    db: Session = Depends(get_db),
) -> PromptTemplateRepository:
    return PromptTemplateRepository(db)


def get_audit_repository(
    db: Session = Depends(get_db),
) -> AuditRepository:
    return AuditRepository(db)


def get_audit_service(
    repository: AuditRepository = Depends(get_audit_repository),
) -> AuditService:
    return AuditService(repository=repository)


def get_prompt_template_service(
    repository: PromptTemplateRepository = Depends(get_prompt_template_repository),
    audit_service: AuditService = Depends(get_audit_service),
) -> PromptTemplateService:
    return PromptTemplateService(repository=repository, audit_service=audit_service)
