from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.encryption_service import EncryptionService
from app.db.session import get_db
from app.dependencies.oauth import get_encryption_service
from app.dependencies.prompt_template import get_audit_service
from app.repositories.ai_provider_config_repository import AIProviderConfigRepository
from app.services.ai_provider_config_service import AIProviderConfigService
from app.services.audit_service import AuditService


def get_ai_provider_config_repository(db: Session = Depends(get_db)) -> AIProviderConfigRepository:
    return AIProviderConfigRepository(db)


def get_ai_provider_config_service(
    repository: AIProviderConfigRepository = Depends(get_ai_provider_config_repository),
    encryption_service: EncryptionService = Depends(get_encryption_service),
    audit_service: AuditService = Depends(get_audit_service),
) -> AIProviderConfigService:
    return AIProviderConfigService(repository, encryption_service, audit_service)
