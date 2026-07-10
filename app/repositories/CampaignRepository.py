from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload
from app.models.pipeline import CampaignCandidate, PipelineStage
from app.models.campaigns import CampaignStatus, HiringCampaign
from datetime import datetime, timezone, timedelta

from app.schemas.campaign.campaign_filter_schema import CampaignFilterRequest

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
    
    def get_all_campaigns(self, show_closed: bool = False) -> list[HiringCampaign]:
        stmt = (
            select(HiringCampaign)
            # .where(
            #     HiringCampaign.status == "ACTIVE",
            # )
            .options(joinedload(HiringCampaign.job_description))
            .order_by(HiringCampaign.created_at.desc())
        )
        if not show_closed:
            stmt = stmt.where(
                HiringCampaign.status != CampaignStatus.CLOSED
            )
        result = self.db.execute(stmt)
        return result.scalars().all()
    
    def get_all_campaigns_for_hrAdmin(self, manager_id: UUID) -> list[HiringCampaign]:
        stmt = (
            select(HiringCampaign)
            .where(
                HiringCampaign.created_by == manager_id,
            )
            .options(joinedload(HiringCampaign.job_description))
            .order_by(HiringCampaign.created_at.desc())
        )
        result = self.db.execute(stmt)
        return result.scalars().all()

    def get_all_campaigns_for_hiring_manager(self, manager_id: UUID) -> list[HiringCampaign]:
        stmt = (
            select(HiringCampaign)
            .where(
                HiringCampaign.hiring_manager_id == manager_id,
            )
            .options(joinedload(HiringCampaign.job_description))
            .order_by(HiringCampaign.created_at.desc())
        )
        result = self.db.execute(stmt)
        return result.scalars().all()
    
    def get_candidate_count(
        self,
        campaign_id: UUID,
    ) -> int:
        """
        Returns total candidates in a campaign.
        """
        return (
            self.db.query(func.count(CampaignCandidate.id))
            .filter(
                CampaignCandidate.campaign_id == campaign_id,
            )
            .scalar()
            or 0
        )
    
    def get_shortlisted_count(
        self,
        campaign_id: UUID,
    ) -> int:
        """
        Returns total shortlisted candidates in a campaign.
        """
        return (
            self.db.query(func.count(CampaignCandidate.id))
            .filter(
                CampaignCandidate.campaign_id == campaign_id,
                CampaignCandidate.pipeline_stage == PipelineStage.SHORTLISTED,
            )
            .scalar()
            or 0
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


    def get_expired_campaigns(self) -> list[HiringCampaign]:
        """
        Returns all ACTIVE campaigns whose deadline has passed.
        """
        return (
            self.db.query(HiringCampaign)
            .filter(
                HiringCampaign.status == CampaignStatus.ACTIVE,
                HiringCampaign.deadline.isnot(None),
                HiringCampaign.deadline < datetime.now(timezone.utc),
            )
            .all()
        )
    
    def close_campaign(self, campaign: HiringCampaign) -> HiringCampaign:
        campaign.status = CampaignStatus.CLOSED
        campaign.updated_at = datetime.now(timezone.utc)

        self.db.flush()
        self.db.refresh(campaign)

        return campaign
    
    def search_campaigns(
        self,
        filters: CampaignFilterRequest,
    ) -> list[HiringCampaign]:

        stmt = (
            select(HiringCampaign)
            .options(
                joinedload(HiringCampaign.job_description),
            )
        )

        # Hide closed campaigns by default
        if not filters.show_closed:
            stmt = stmt.where(
                HiringCampaign.status != CampaignStatus.CLOSED
            )

        # Search by campaign name
        if filters.search:
            stmt = stmt.where(
                HiringCampaign.name.ilike(f"%{filters.search}%")
            )

        # Filter by status
        if filters.status:
            stmt = stmt.where(
                HiringCampaign.status == filters.status
            )

        # Filter by Hiring Manager
        if filters.hiring_manager_id:
            stmt = stmt.where(
                HiringCampaign.hiring_manager_id
                == filters.hiring_manager_id
            )

        # Filter by JD
        if filters.jd_id:
            stmt = stmt.where(
                HiringCampaign.jd_id == filters.jd_id
            )

        # Filter by deadline
        if filters.has_deadline is True:
            stmt = stmt.where(
                HiringCampaign.deadline.is_not(None)
            )

        elif filters.has_deadline is False:
            stmt = stmt.where(
                HiringCampaign.deadline.is_(None)
            )

        stmt = stmt.order_by(
            HiringCampaign.created_at.desc()
        )

        result = self.db.execute(stmt)

        return result.scalars().all()
    
    def is_deadline_soon(
        self,
        campaign: HiringCampaign,
        warning_days: int = 3,
    ) -> bool:

        if campaign.deadline is None:
            return False

        now = datetime.now(timezone.utc)

        return now <= campaign.deadline <= now + timedelta(days=warning_days)