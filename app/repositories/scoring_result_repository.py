"""candidate_scoring_results - one row per deterministic scoring per version (v1 / v2)."""
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.orm import Session

from app.models.scoring_v2 import CandidateScoringResult


class ScoringResultRepository:
    def __init__(self, db: Session):
        self.db = db

    def add(self, row: CandidateScoringResult) -> None:
        """Inside a savepoint: a failed insert never aborts the scoring transaction."""
        with self.db.begin_nested():
            self.db.add(row)

    def v2_llm_calls_today(self) -> int:
        """Core-confirmation LLM calls recorded by today's (UTC) v2 results - the daily cap's counter."""
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        calls = cast(CandidateScoringResult.breakdown["confirmation"]["llm_calls"].astext, Integer)
        total = self.db.execute(
            select(func.coalesce(func.sum(calls), 0))
            .where(CandidateScoringResult.scoring_version == "v2", CandidateScoringResult.created_at >= start)
        ).scalar_one()
        return int(total or 0)

    def latest(self, campaign_candidate_id: UUID, scoring_version: str) -> CandidateScoringResult | None:
        return self.db.execute(
            select(CandidateScoringResult)
            .where(CandidateScoringResult.campaign_candidate_id == campaign_candidate_id,
                   CandidateScoringResult.scoring_version == scoring_version)
            .order_by(CandidateScoringResult.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
