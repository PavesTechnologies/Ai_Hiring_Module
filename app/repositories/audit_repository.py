from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.compliance import AuditLog


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

    def save(self):
        self.db.commit()