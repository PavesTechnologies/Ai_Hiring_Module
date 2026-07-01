from uuid import UUID

from app.enums.constants import (
    ActionType,
    EntityType,
)

from app.models.compliance import AuditLog
from app.repositories.audit_repository import AuditRepository

class AuditService:
    def __init__(self, repository: AuditRepository):
        self.repository = repository
    
    
    def log(
        self,
        *,
        actor_id: UUID,
        actor_role: str | None,
        action_type: str,
        entity_type: str,
        entity_id: UUID,
        details: dict | None = None,
        campaign_id: UUID | None = None,
        jurisdiction: str | None = None,
        ip_address: str | None = None,
        session_id: UUID | None = None,request_id: UUID | None = None,
        ) -> AuditLog:
        audit = AuditLog(
            actor_id=actor_id,
            actor_role=actor_role,
            action_type=action_type,
            entity_type=entity_type,
            entity_id=entity_id,
            campaign_id=campaign_id,
            jurisdiction=jurisdiction,
            detail=details,
            ip_address=ip_address,
            session_id=session_id,
            request_id=request_id,
            )
        return self.repository.create(audit)