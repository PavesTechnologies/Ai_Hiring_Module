from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.pipeline import AllowedTransition, PipelineStage


class AllowedTransitionRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(
        self,
        from_stage: PipelineStage,
        to_stage: PipelineStage,
        previous_stage: PipelineStage | None = None,
    ) -> AllowedTransition | None:
        """
        Governance model (2026-08-31): resolves the row that actually
        governs this (previous_stage, from_stage, to_stage) move. An exact
        previous_stage match takes priority (e.g. HM_REVIEW ownership
        flips) - if none exists, falls back to the NULL/wildcard row for
        the same (from_stage, to_stage) pair, which is what every caller
        that doesn't yet track previous_stage (previous_stage=None) always
        resolves to.
        """
        if previous_stage is not None:
            exact = self.db.execute(
                select(AllowedTransition).where(
                    AllowedTransition.previous_stage == previous_stage,
                    AllowedTransition.from_stage == from_stage,
                    AllowedTransition.to_stage == to_stage,
                )
            ).scalars().first()
            if exact is not None:
                return exact

        return self.db.execute(
            select(AllowedTransition).where(
                AllowedTransition.previous_stage.is_(None),
                AllowedTransition.from_stage == from_stage,
                AllowedTransition.to_stage == to_stage,
            )
        ).scalars().first()

    def is_transition_allowed(
        self,
        from_stage: PipelineStage,
        to_stage: PipelineStage,
        previous_stage: PipelineStage | None = None,
    ) -> bool:
        """
        M07-E03 S02 T01: existence check only - allowed_transitions
        governs whether a (previous_stage, from_stage, to_stage) triple is
        configured at all, independent of allowed_roles (a separate,
        human-permission concern for manual transitions, not checked here
        for a SYSTEM-initiated one). Same exact-then-wildcard resolution as
        get().
        """
        return self.get(from_stage, to_stage, previous_stage=previous_stage) is not None

    def list_eligible(
        self,
        from_stage: PipelineStage,
        previous_stage: PipelineStage | None = None,
    ) -> list[AllowedTransition]:
        """
        Every transition configured out of from_stage for a candidate that
        arrived there from previous_stage - the list form of get(), with the
        same exact-then-wildcard resolution applied per to_stage (an exact
        previous_stage row shadows the NULL/wildcard row for that same
        to_stage, so a target is never returned twice with two different
        allowed_roles). Read-only; the role filter is a caller concern,
        not this repository's - it returns what the table configures.
        """
        rows = self.db.execute(
            select(AllowedTransition).where(
                AllowedTransition.from_stage == from_stage,
                or_(
                    AllowedTransition.previous_stage.is_(None),
                    AllowedTransition.previous_stage == previous_stage,
                ),
            )
        ).scalars().all()

        resolved: dict[PipelineStage, AllowedTransition] = {}
        for row in rows:
            if row.previous_stage is not None or row.to_stage not in resolved:
                resolved[row.to_stage] = row
        return list(resolved.values())
