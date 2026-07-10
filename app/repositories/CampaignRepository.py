from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from datetime import datetime, timezone

from app.models.campaigns import CampaignStatus, HiringCampaign
from app.models.compliance import AuditLog
from app.models.skills import JDSkill
from app.models.pipeline import CampaignCandidate, CampaignCandidateStageHistory
from app.models.identity import User, UserRole

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
    
    def get_all_campaigns(self) -> list[HiringCampaign]:
        stmt = (
            select(HiringCampaign)
            # .where(
            #     HiringCampaign.status == "ACTIVE",
            # )
            .options(joinedload(HiringCampaign.job_description))
            .order_by(HiringCampaign.created_at.desc())
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

    def get_mandatory_skill_count(self, jd_id) -> int:
        return (
            self.db.query(JDSkill)
            .filter(JDSkill.jd_id == jd_id, JDSkill.mandatory == True)
            .count()
        )
    
    def get_candidate_count(self,campaign_id) -> int:
        return (
            self.db.query(CampaignCandidate)
            .filter(CampaignCandidate.campaign_id == campaign_id)
            .count()
        )
    def get_user(self, user_id: str) -> User | None:
        return self.db.get(User, user_id)

    def get_stage_counts(self, campaign_id) -> dict[str, int]:
        rows = (
            self.db.query(CampaignCandidate.pipeline_stage, func.count())
            .filter(CampaignCandidate.campaign_id == campaign_id)
            .group_by(CampaignCandidate.pipeline_stage)
            .all()
        )
        return {stage.value: count for stage, count in rows}

    def get_audit_entries(self, campaign_id) -> list[AuditLog]:
        return (
            self.db.query(AuditLog)
            .filter(AuditLog.campaign_id == campaign_id)
            .order_by(AuditLog.created_at.desc())
            .all()
        )

    def get_stage_history(self, campaign_id) -> list[CampaignCandidateStageHistory]:
        return (
            self.db.query(CampaignCandidateStageHistory)
            .join(
                CampaignCandidate,
                CampaignCandidateStageHistory.campaign_candidate_id == CampaignCandidate.id,
            )
            .filter(CampaignCandidate.campaign_id == campaign_id)
            .order_by(CampaignCandidateStageHistory.changed_at.desc())
            .all()
        )