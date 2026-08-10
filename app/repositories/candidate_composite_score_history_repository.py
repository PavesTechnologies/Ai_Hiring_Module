from uuid import UUID

from sqlalchemy import delete, select
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
        never updated in the normal write path, so there is no
        corresponding update() method here. delete_by_campaign_candidate_id
        below is the one deliberate exception (candidate erasure).
        """
        self.db.add(history)
        self.db.flush()
        self.db.refresh(history)
        return history

    def delete_by_campaign_candidate_id(self, campaign_candidate_id: UUID) -> None:
        """
        Candidate erasure — candidate_composite_score_history.campaign_candidate_id
        is a NOT NULL FK to campaign_candidates.id, so this must run before
        the campaign_candidate row itself is deleted.
        """
        self.db.execute(
            delete(CandidateCompositeScoreHistory).where(
                CandidateCompositeScoreHistory.campaign_candidate_id == campaign_candidate_id
            )
        )
        self.db.flush()

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
