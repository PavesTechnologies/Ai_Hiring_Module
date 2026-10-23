from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.upload_decisions import UploadDecision, UploadDecisionStatus


class UploadDecisionRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, decision: UploadDecision) -> UploadDecision:
        self.db.add(decision)
        self.db.flush()
        return decision

    def get_by_id(self, decision_id: UUID) -> UploadDecision | None:
        return self.db.get(UploadDecision, decision_id)

    def get_by_id_for_update(self, decision_id: UUID) -> UploadDecision | None:
        """Locked for the rest of the transaction - two people deciding at once can't both act."""
        stmt = select(UploadDecision).where(UploadDecision.id == decision_id).with_for_update()
        return self.db.execute(stmt).scalars().first()

    def list_by_campaign(self, campaign_id: UUID, status: str | None = UploadDecisionStatus.PENDING.value
                         ) -> list[UploadDecision]:
        """Newest first; status None means every status."""
        stmt = select(UploadDecision).where(UploadDecision.campaign_id == campaign_id)
        if status is not None:
            stmt = stmt.where(UploadDecision.status == status)
        return list(self.db.execute(stmt.order_by(UploadDecision.created_at.desc())).scalars().all())

    def list_by_bulk_upload_job(self, bulk_upload_job_id: UUID) -> list[UploadDecision]:
        stmt = (select(UploadDecision)
                .where(UploadDecision.bulk_upload_job_id == bulk_upload_job_id)
                .order_by(UploadDecision.created_at.desc()))
        return list(self.db.execute(stmt).scalars().all())

    def list_expired_pending(self, now: datetime) -> list[UploadDecision]:
        stmt = select(UploadDecision).where(
            UploadDecision.status == UploadDecisionStatus.PENDING.value,
            UploadDecision.expires_at <= now,
        )
        return list(self.db.execute(stmt).scalars().all())

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
