from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.campaigns import CampaignWeightConfigurationHistory


class CampaignWeightConfigurationHistoryRepository:
    """
    M10-E02: repository for campaign_weight_configuration_history - the
    append-only audit trail of every Campaign Weight Configuration change.
    Mirrors CandidateCompositeScoreHistoryRepository's exact shape (M10-E01):
    a create() for the one INSERT this table ever receives, a read method
    for retrieving a campaign's history, and no update()/delete() at all -
    rows in this table are never mutated or removed.
    """

    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        history: CampaignWeightConfigurationHistory,
    ) -> CampaignWeightConfigurationHistory:
        """Append-only insert - one row per actual weight change, never updated or deleted."""
        self.db.add(history)
        self.db.flush()
        self.db.refresh(history)
        return history

    def get_by_campaign_id(
        self,
        campaign_id: UUID,
    ) -> list[CampaignWeightConfigurationHistory]:
        """Full weight-configuration change history for one campaign, most recent first."""
        stmt = (
            select(CampaignWeightConfigurationHistory)
            .where(CampaignWeightConfigurationHistory.campaign_id == campaign_id)
            .order_by(CampaignWeightConfigurationHistory.changed_at.desc())
        )
        return list(self.db.execute(stmt).scalars().all())

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
