from datetime import datetime
from uuid import UUID

from app.enums.constants import (
    ActionType,
    EntityType,
)
from app.models.campaigns import CampaignStatus

from app.models.compliance import AuditLog
from app.repositories.audit_repository import AuditRepository

class AuditService:
    def __init__(self, repository: AuditRepository):
        self.repository = repository
    
    
    def log(
        self,
        *,
        actor_id: str,
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
    
    def get_campaign_scoring_history(
        self,
        campaign_id: UUID,
    ):
        return self.repository.get_campaign_scoring_history(
            campaign_id
        )

    def get_all_scoring_changes(
        self,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        campaign_status: CampaignStatus | None = None,
    ):
        """S05-T03: cross-campaign feed for the Weight Change Report."""
        return self.repository.get_all_scoring_changes(
            date_from=date_from,
            date_to=date_to,
            campaign_status=campaign_status,
        )