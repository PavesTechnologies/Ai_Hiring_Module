from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.pipeline import CampaignCandidate, CandidateRejection, RejectionLayer


class CandidateRejectionRepository:

    def __init__(self, db: Session):
        self.db = db

    def create(self, rejection: CandidateRejection) -> CandidateRejection:
        self.db.add(rejection)
        self.db.flush()
        self.db.refresh(rejection)
        return rejection

    def get_by_campaign_candidate_id(self, campaign_candidate_id: UUID) -> list[CandidateRejection]:
        return (
            self.db.query(CandidateRejection)
            .filter(CandidateRejection.campaign_candidate_id == campaign_candidate_id)
            .order_by(CandidateRejection.rejected_at.desc())
            .all()
        )

    def get_by_campaign(
        self,
        campaign_id: UUID | None = None,
        rejection_layer: RejectionLayer | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> list[tuple[CandidateRejection, UUID]]:
        """
        M07-E03 S05: every candidate_rejections row (joined onto its
        campaign_candidate to resolve campaign_id), optionally scoped to
        one campaign_id (None = platform-wide, mirroring
        CampaignCandidateRepository.get_overridden's convention),
        rejection_layer and/or a rejected_at date range - backs the
        campaign Rejection Analytics report and the platform-wide export.
        Returns (CandidateRejection, campaign_id) pairs so a platform-wide
        caller can group by campaign without a second query per campaign.
        """
        stmt = (
            select(CandidateRejection, CampaignCandidate.campaign_id)
            .join(CampaignCandidate, CandidateRejection.campaign_candidate_id == CampaignCandidate.id)
        )
        if campaign_id is not None:
            stmt = stmt.where(CampaignCandidate.campaign_id == campaign_id)
        if rejection_layer is not None:
            stmt = stmt.where(CandidateRejection.rejection_layer == rejection_layer)
        if date_from is not None:
            stmt = stmt.where(CandidateRejection.rejected_at >= date_from)
        if date_to is not None:
            stmt = stmt.where(CandidateRejection.rejected_at <= date_to)
        return self.db.execute(stmt).all()

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
