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

    def auto_close_expired_campaigns(self) -> int:
        """
        Auto close campaigns whose deadline has expired.

        Returns:
            int: Number of campaigns closed.
        """

        try:
            expired_campaigns = self.campaign_repo.get_expired_campaigns()

            if not expired_campaigns:
                return 0

            closed_count = 0

            for campaign in expired_campaigns:

                self.campaign_repo.close_campaign(campaign)

                # Audit log
                self.audit_service.log(
                    actor_id=SYSTEM,
                    actor_role="SYSTEM",
                    action_type=ActionType.CAMPAIGN_AUTO_CLOSED.value,
                    entity_type=EntityType.CAMPAIGN.value,
                    entity_id=campaign.id,
                    campaign_id=campaign.id,
                    details={
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

                closed_count += 1

            self.campaign_repo.commit()

            return closed_count

        except Exception:
            self.campaign_repo.rollback()
            raise