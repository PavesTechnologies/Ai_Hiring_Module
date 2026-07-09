from uuid import UUID

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.pipeline import CampaignCandidate


class CampaignCandidateRepository:

    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        campaign_candidate: CampaignCandidate,
    ) -> CampaignCandidate:
        """
        Create a new campaign candidate.
        """
        self.db.add(campaign_candidate)
        self.db.flush()
        self.db.refresh(campaign_candidate)
        return campaign_candidate

    def get_by_id(
        self,
        campaign_candidate_id: UUID,
    ) -> CampaignCandidate | None:
        """
        Get campaign candidate by ID.
        """
        return (
            self.db.query(CampaignCandidate)
            .filter(CampaignCandidate.id == campaign_candidate_id)
            .first()
        )
    
    def get_by_campaign_and_candidate(
        self,
        campaign_id: UUID,
        candidate_id: UUID,
    ) -> CampaignCandidate | None:
        """
        Returns campaign candidate if already exists.
        """

        return (
            self.db.query(CampaignCandidate)
            .filter(
                CampaignCandidate.campaign_id == campaign_id,
                CampaignCandidate.candidate_id == candidate_id,
            )
            .first()
        )

    def get_candidate_count(
        self,
        campaign_id: UUID,
    ) -> int:
        """
        Returns total number of candidates in a campaign.
        """
        return (
            self.db.query(func.count(CampaignCandidate.id))
            .filter(CampaignCandidate.campaign_id == campaign_id)
            .scalar()
            or 0
        )

    def get_all_by_campaign(
        self,
        campaign_id: UUID,
    ) -> list[CampaignCandidate]:
        """
        Returns all candidates belonging to a campaign.
        """
        stmt = (
            select(CampaignCandidate)
            .where(CampaignCandidate.campaign_id == campaign_id)
            .order_by(CampaignCandidate.created_at.desc())
        )

        result = self.db.execute(stmt)

        return result.scalars().all()

    def update(
        self,
        campaign_candidate: CampaignCandidate,
    ) -> CampaignCandidate:
        """
        Update campaign candidate.
        """
        self.db.flush()
        self.db.refresh(campaign_candidate)
        return campaign_candidate

    def delete(
        self,
        campaign_candidate: CampaignCandidate,
    ) -> None:
        """
        Delete campaign candidate.
        """
        self.db.delete(campaign_candidate)
        self.db.flush()

    def commit(self) -> None:
        """
        Commit transaction.
        """
        self.db.commit()

    def rollback(self) -> None:
        """
        Rollback transaction.
        """
        self.db.rollback()