"""candidate_scoring_results - one row per deterministic scoring per version (v1 / v2)."""
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.scoring_v2 import CandidateScoringResult


class ScoringResultRepository:
    def __init__(self, db: Session):
        self.db = db

    def add(self, row: CandidateScoringResult) -> None:
        """Inside a savepoint: a failed insert never aborts the scoring transaction."""
        with self.db.begin_nested():
            self.db.add(row)

    def latest(self, campaign_candidate_id: UUID, scoring_version: str) -> CandidateScoringResult | None:
        return self.db.execute(
            select(CandidateScoringResult)
            .where(CandidateScoringResult.campaign_candidate_id == campaign_candidate_id,
                   CandidateScoringResult.scoring_version == scoring_version)
            .order_by(CandidateScoringResult.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    def latest_acted_on(self, campaign_candidate_id: UUID) -> CandidateScoringResult | None:
        """The latest result the pipeline acted on - never a candidate (shadow) scoring version's row."""
        return self.db.execute(
            select(CandidateScoringResult)
            .where(CandidateScoringResult.campaign_candidate_id == campaign_candidate_id,
                   CandidateScoringResult.acted_on.is_(True))
            .order_by(CandidateScoringResult.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    def awaiting_full_scoring(self, limit: int = 50) -> list[CandidateScoringResult]:
        """Acted-on no-LLM results that held their candidate for review, oldest first (phase B)."""
        return list(self.db.execute(
            select(CandidateScoringResult)
            .where(CandidateScoringResult.awaiting_full_scoring.is_(True), CandidateScoringResult.acted_on.is_(True))
            .order_by(CandidateScoringResult.created_at)
            .limit(limit)
        ).scalars().all())

    def get(self, result_id: UUID) -> CandidateScoringResult | None:
        return self.db.get(CandidateScoringResult, result_id)
