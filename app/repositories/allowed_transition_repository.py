from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.pipeline import AllowedTransition, PipelineStage


class AllowedTransitionRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, from_stage: PipelineStage, to_stage: PipelineStage) -> AllowedTransition | None:
        stmt = select(AllowedTransition).where(
            AllowedTransition.from_stage == from_stage,
            AllowedTransition.to_stage == to_stage,
        )
        return self.db.execute(stmt).scalars().first()
