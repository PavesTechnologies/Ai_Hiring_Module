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

    def is_transition_allowed(self, from_stage: PipelineStage, to_stage: PipelineStage) -> bool:
        """
        M07-E03 S02 T01: existence check only - allowed_transitions
        governs whether a (from_stage, to_stage) pair is configured at
        all, independent of allowed_roles (a separate, human-permission
        concern for manual transitions, not checked here for a
        SYSTEM-initiated one).
        """
        return (
            self.db.query(AllowedTransition)
            .filter(
                AllowedTransition.from_stage == from_stage,
                AllowedTransition.to_stage == to_stage,
            )
            .first()
        ) is not None
