from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.compliance import AuditLog
from app.models.campaigns import CampaignStatus, HiringCampaign
from app.models.identity import User


class AuditRepository:

    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        audit_log: AuditLog,
    ) -> AuditLog:

        self.db.add(audit_log)
        self.db.flush()
        self.db.refresh(audit_log)

        return audit_log

    def get_campaign_scoring_history(
        self,
        campaign_id: UUID,
    ) -> list[AuditLog]:

        stmt = (
            select(AuditLog)
            .where(
                AuditLog.campaign_id == campaign_id,
                # CAMPAIGN_THRESHOLDS_UPDATED is kept here for backward
                # compatibility with rows written before update_scoring_configuration
                # was switched to log CAMPAIGN_SCORING_CONFIG_CHANGED like every
                # other scoring-edit path — new rows only ever use the latter.
                AuditLog.action_type.in_(
                    ["CAMPAIGN_SCORING_CONFIG_CHANGED", "CAMPAIGN_THRESHOLDS_UPDATED"]
                ),
            )
            .order_by(
                AuditLog.created_at.desc()
            )
        )

        result = self.db.execute(stmt)

        return result.scalars().all()

    def get_all_scoring_changes(
        self,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        campaign_status: CampaignStatus | None = None,
    ) -> list:
        """
        S05-T03: every CAMPAIGN_SCORING_CONFIG_CHANGED entry across the whole
        org, joined to hiring_campaigns (name, status — for filtering) and
        users (actor full_name), for the consolidated Weight Change Report.
        """
        stmt = (
            select(AuditLog, HiringCampaign.name, HiringCampaign.status, User.full_name)
            .join(HiringCampaign, HiringCampaign.id == AuditLog.campaign_id)
            .outerjoin(User, User.id == AuditLog.actor_id)
            .where(
                AuditLog.action_type.in_(
                    ["CAMPAIGN_SCORING_CONFIG_CHANGED", "CAMPAIGN_THRESHOLDS_UPDATED"]
                )
            )
            .order_by(AuditLog.created_at.desc())
        )

        if date_from is not None:
            stmt = stmt.where(AuditLog.created_at >= date_from)
        if date_to is not None:
            stmt = stmt.where(AuditLog.created_at <= date_to)
        if campaign_status is not None:
            stmt = stmt.where(HiringCampaign.status == campaign_status)

        return self.db.execute(stmt).all()

    def get_latest_entry(
        self,
        campaign_id: UUID,
        action_type: str,
    ) -> AuditLog | None:
        """most recent audit entry of a given type for a campaign — used to compute pause duration on resume."""
        stmt = (
            select(AuditLog)
            .where(
                AuditLog.campaign_id == campaign_id,
                AuditLog.action_type == action_type,
            )
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        )
        return self.db.execute(stmt).scalars().first()

    def save(self):
        self.db.commit()