from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.async_tasks import (
    DocumentProcessingStageExecution,
    DocumentType,
    ProcessingStage,
    StageExecutionStatus,
)


class DocumentProcessingRepository:
    """
    Generic (document-type-agnostic) CRUD for per-stage pipeline progress.
    Grouped by `task_id` so a poller can reconstruct the full stage timeline
    for one submission before the underlying document row even exists.
    """

    def __init__(self, db: Session):
        self.db = db

    def start_stage(
        self,
        task_id: str,
        document_type: DocumentType,
        stage: ProcessingStage,
        attempt_number: int = 1,
    ) -> DocumentProcessingStageExecution:
        """
        Idempotent on (task_id, stage, attempt_number) — the table's own
        unique constraint. A broker-level redelivery after an ungraceful
        worker crash mid-stage re-enters this same triple (Celery's own
        retry counter, and therefore attempt_number, never incremented,
        since this isn't a self.retry()-triggered retry); inserting again
        would crash on the unique constraint instead of just resuming.
        Reset the existing row in that case rather than inserting a
        duplicate.
        """
        existing = (
            self.db.query(DocumentProcessingStageExecution)
            .filter(
                DocumentProcessingStageExecution.task_id == task_id,
                DocumentProcessingStageExecution.stage == stage,
                DocumentProcessingStageExecution.attempt_number == attempt_number,
            )
            .first()
        )
        if existing is not None:
            existing.document_type = document_type
            existing.status = StageExecutionStatus.RUNNING
            existing.error_message = None
            existing.duration_ms = None
            existing.started_at = datetime.now(timezone.utc)
            existing.completed_at = None
            self.db.flush()
            self.db.refresh(existing)
            return existing

        execution = DocumentProcessingStageExecution(
            task_id=task_id,
            document_type=document_type,
            stage=stage,
            status=StageExecutionStatus.RUNNING,
            attempt_number=attempt_number,
            started_at=datetime.now(timezone.utc),
        )
        self.db.add(execution)
        self.db.flush()
        self.db.refresh(execution)
        return execution

    def complete_stage(
        self,
        execution: DocumentProcessingStageExecution,
        status: StageExecutionStatus,
        error_message: str | None = None,
        duration_ms: int | None = None,
    ) -> DocumentProcessingStageExecution:
        execution.status = status
        execution.error_message = error_message
        execution.duration_ms = duration_ms
        execution.completed_at = datetime.now(timezone.utc)
        self.db.flush()
        self.db.refresh(execution)
        return execution

    def get_latest_attempt_number(self, task_id: str, stage: ProcessingStage) -> int:
        """
        Retry hook: the highest attempt_number recorded for this (task_id,
        stage) pair, or 0 if the stage has never run. Not called by any
        current code path — available for a future retry driver.
        """
        latest = (
            self.db.query(func.max(DocumentProcessingStageExecution.attempt_number))
            .filter(
                DocumentProcessingStageExecution.task_id == task_id,
                DocumentProcessingStageExecution.stage == stage,
            )
            .scalar()
        )
        return latest or 0

    def get_by_task_id(self, task_id: str) -> list[DocumentProcessingStageExecution]:
        return (
            self.db.query(DocumentProcessingStageExecution)
            .filter(DocumentProcessingStageExecution.task_id == task_id)
            .order_by(DocumentProcessingStageExecution.created_at)
            .all()
        )

    def get_by_task_ids(self, task_ids: list[str]) -> list[DocumentProcessingStageExecution]:
        """
        Batched counterpart to get_by_task_id — one query for a whole page
        of tasks (e.g. a "my uploads" list) instead of one query per task.
        Caller groups the flat result by task_id.
        """
        if not task_ids:
            return []
        return (
            self.db.query(DocumentProcessingStageExecution)
            .filter(DocumentProcessingStageExecution.task_id.in_(task_ids))
            .order_by(DocumentProcessingStageExecution.task_id, DocumentProcessingStageExecution.created_at)
            .all()
        )

    def get_avg_duration_by_stage_since(self, since: datetime, document_type: DocumentType) -> dict[str, float]:
        """Monitoring-only. Backs processing-metrics' bounded-window avg_duration_by_stage."""
        stmt = (
            select(
                DocumentProcessingStageExecution.stage,
                func.avg(DocumentProcessingStageExecution.duration_ms),
            )
            .where(
                DocumentProcessingStageExecution.document_type == document_type,
                DocumentProcessingStageExecution.created_at >= since,
                DocumentProcessingStageExecution.duration_ms.is_not(None),
            )
            .group_by(DocumentProcessingStageExecution.stage)
        )
        return {stage.value: round(float(avg_ms), 1) for stage, avg_ms in self.db.execute(stmt).all()}

    def get_failure_rate_by_stage_since(self, since: datetime, document_type: DocumentType) -> dict[str, float]:
        """Monitoring-only. FAILED-count / total-count per stage, within the bounded window."""
        stmt = (
            select(
                DocumentProcessingStageExecution.stage,
                func.count().filter(DocumentProcessingStageExecution.status == StageExecutionStatus.FAILED),
                func.count(),
            )
            .where(
                DocumentProcessingStageExecution.document_type == document_type,
                DocumentProcessingStageExecution.created_at >= since,
            )
            .group_by(DocumentProcessingStageExecution.stage)
        )
        return {
            stage.value: round(failed / total, 4) if total else 0.0
            for stage, failed, total in self.db.execute(stmt).all()
        }

    def link_document_id(self, task_id: str, document_id: UUID) -> None:
        (
            self.db.query(DocumentProcessingStageExecution)
            .filter(DocumentProcessingStageExecution.task_id == task_id)
            .update({DocumentProcessingStageExecution.document_id: document_id})
        )
        self.db.flush()

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
