from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.pipeline import CandidateCompositeScoreHistory


class CandidateCompositeScoreHistoryRepository:

    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        history: CandidateCompositeScoreHistory,
    ) -> CandidateCompositeScoreHistory:
        """
        Append-only insert - candidate_composite_score_history rows are
        never updated or deleted, so there is no corresponding update()/
        delete() method on this repository.
        """
        self.db.add(history)
        self.db.flush()
        self.db.refresh(history)
        return history

    def get_by_campaign_candidate_id(
        self,
        campaign_candidate_id: UUID,
    ) -> list[CandidateCompositeScoreHistory]:
        """Full calculation history for one candidate, most recent first."""
        stmt = (
            select(CandidateCompositeScoreHistory)
            .where(CandidateCompositeScoreHistory.campaign_candidate_id == campaign_candidate_id)
            .order_by(CandidateCompositeScoreHistory.calculated_at.desc())
        )
        return list(self.db.execute(stmt).scalars().all())

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
