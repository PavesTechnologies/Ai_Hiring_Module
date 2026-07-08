from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.campaigns import HiringCampaign


class CampaignRepository:

    def __init__(self, db: Session):
        self.db = db

    def create_campaign(
        self,
        campaign: HiringCampaign,
    ) -> HiringCampaign:
        self.db.add(campaign)
        self.db.flush()
        self.db.refresh(campaign)
        return campaign

    def get_by_id(
        self,
        campaign_id: UUID,
    ) -> HiringCampaign | None:
        return (
            self.db.query(HiringCampaign)
            .filter(HiringCampaign.id == campaign_id)
            .first()
        )

    def get_by_name(
        self,
        org_id: UUID,
        name: str,
    ) -> HiringCampaign | None:
        return (
            self.db.query(HiringCampaign)
            .filter(
                HiringCampaign.org_id == org_id,
                func.lower(HiringCampaign.name) == name.lower(),
            )
            .first()
        )

    def get_all_by_org(
        self,
        org_id: UUID,
    ) -> list[HiringCampaign]:
        return (
            self.db.query(HiringCampaign)
            .filter(HiringCampaign.org_id == org_id)
            .all()
        )

    def update(self, campaign: HiringCampaign) -> HiringCampaign:
        """Update an existing campaign and refresh it."""
        self.db.flush()
        self.db.refresh(campaign)
        return campaign

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()