from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.async_tasks import CeleryTaskLog, FailureClassification, ProcessingStage, StageFailureLog


class StageFailureLogRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_task_id(self, task_id: str) -> list[StageFailureLog]:
        """Read-only — monitoring lookup, no writes. Every failed attempt, not just the final one."""
        stmt = (
            select(StageFailureLog)
            .where(StageFailureLog.task_id == task_id)
            .order_by(StageFailureLog.created_at)
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_by_task_ids(self, task_ids: list[str]) -> list[StageFailureLog]:
        """Batched counterpart to get_by_task_id — one query for a whole job's files, not one per file."""
        if not task_ids:
            return []
        stmt = (
            select(StageFailureLog)
            .where(StageFailureLog.task_id.in_(task_ids))
            .order_by(StageFailureLog.task_id, StageFailureLog.created_at)
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_top_failure_reasons_since(
        self, since: datetime, task_types: list[str], limit: int = 5,
    ) -> list[tuple[str, int]]:
        """
        Monitoring-only. stage_failure_logs carries no document_type of its
        own, so scoping to resume-intake failures (excluding JD processing,
        etc.) goes through a join to celery_task_log.task_type instead.
        """
        stmt = (
            select(StageFailureLog.exception_type, func.count())
            .join(CeleryTaskLog, CeleryTaskLog.task_id == StageFailureLog.task_id)
            .where(
                StageFailureLog.created_at >= since,
                CeleryTaskLog.task_type.in_(task_types),
            )
            .group_by(StageFailureLog.exception_type)
            .order_by(func.count().desc())
            .limit(limit)
        )
        return [(reason, count) for reason, count in self.db.execute(stmt).all()]

    def record(
        self,
        task_id: str,
        stage: ProcessingStage,
        attempt_number: int,
        exception_type: str,
        message: str,
        classification: FailureClassification,
    ) -> StageFailureLog:
        log = StageFailureLog(
            task_id=task_id,
            stage=stage,
            attempt_number=attempt_number,
            exception_type=exception_type,
            message=message,
            classification=classification,
        )
        self.db.add(log)
        self.db.flush()
        self.db.refresh(log)
        return log

    def commit(self) -> None:
        self.db.commit()
