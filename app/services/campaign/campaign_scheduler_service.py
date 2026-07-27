from datetime import datetime, timezone

from app.enums.constants import ActionType, EntityType
from app.repositories.CampaignRepository import CampaignRepository
from app.services.audit_service import AuditService


class CampaignSchedulerService:

    def __init__(
        self,
        campaign_repo: CampaignRepository,
        audit_service: AuditService,
    ):
        self.campaign_repo = campaign_repo
        self.audit_service = audit_service

    def auto_close_expired_campaigns(self, batch_size: int = 100) -> int:
        """
        Auto close campaigns whose deadline has expired.

        E03-S05-T02: processed in batches (each batch committed separately)
        rather than one giant transaction locking every expired row at once
        — the ACTIVE-status filter in get_expired_campaigns() naturally
        excludes campaigns already closed by a prior batch, so no offset
        bookkeeping is needed for the loop to converge.

        Returns:
            int: Number of campaigns closed.
        """

        total_closed = 0

        try:
            while True:
                batch = self.campaign_repo.get_expired_campaigns(limit=batch_size)
                if not batch:
                    break

                for campaign in batch:

                    self.campaign_repo.close_campaign(campaign)

                    # Audit log — attributed to the HR_ADMIN who created the campaign,
                    # since the closure is triggered by the scheduler on their behalf.
                    self.audit_service.log(
                        actor_id=campaign.created_by,
                        actor_role="HR_ADMIN",
                        action_type=ActionType.CAMPAIGN_AUTO_CLOSED.value,
                        entity_type=EntityType.CAMPAIGN.value,
                        entity_id=campaign.id,
                        campaign_id=campaign.id,
                        details={
                            "title": f"Campaign '{campaign.name}' auto-closed",
                            "reason": "DEADLINE_EXPIRED",
                            "deadline": campaign.deadline.isoformat()
                            if campaign.deadline
                            else None,
                            "closed_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )

                    # Email notification
                    # TODO:
                    # Notify HR Admin
                    # Notify Hiring Manager

                    total_closed += 1

                self.campaign_repo.commit()

                if len(batch) < batch_size:
                    break

            return total_closed

        except Exception:
            self.campaign_repo.rollback()
            raise