from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models.async_tasks import (
    DocumentProcessingStageExecution,
    DocumentType,
    ProcessingStage,
    StageExecutionStatus,
    StageFailureLog,
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

    def delete_by_task_id(self, task_id: str) -> None:
        """Dead-letter cleanup — removes every stage-execution row for one task_id."""
        self.db.execute(
            delete(DocumentProcessingStageExecution).where(DocumentProcessingStageExecution.task_id == task_id)
        )
        self.db.flush()

    def link_document_id(self, task_id: str, document_id: UUID) -> None:
        (
            self.db.query(DocumentProcessingStageExecution)
            .filter(DocumentProcessingStageExecution.task_id == task_id)
            .update({DocumentProcessingStageExecution.document_id: document_id})
        )
        self.db.flush()

    def delete_for_documents(self, document_type: DocumentType, document_ids: list[UUID]) -> int:
        """
        Candidate erasure - removes the processing-stage rows of these
        documents (no FK, so nothing else clears them) and the stage-failure
        rows of the same tasks (exception messages). Returns the stage rows deleted.
        """
        if not document_ids:
            return 0
        task_ids = list(self.db.execute(
            select(DocumentProcessingStageExecution.task_id).where(
                DocumentProcessingStageExecution.document_type == document_type,
                DocumentProcessingStageExecution.document_id.in_(document_ids),
            ).distinct()
        ).scalars())
        deleted = self.db.execute(delete(DocumentProcessingStageExecution).where(
            DocumentProcessingStageExecution.document_type == document_type,
            DocumentProcessingStageExecution.document_id.in_(document_ids),
        )).rowcount or 0
        if task_ids:
            # only tasks that processed nothing else (a task parses one document)
            still_used = set(self.db.execute(
                select(DocumentProcessingStageExecution.task_id)
                .where(DocumentProcessingStageExecution.task_id.in_(task_ids)).distinct()
            ).scalars())
            orphaned = [t for t in task_ids if t not in still_used]
            if orphaned:
                self.db.execute(delete(StageFailureLog).where(StageFailureLog.task_id.in_(orphaned)))
        self.db.flush()
        return deleted

    def delete_for_tasks(self, task_ids) -> int:
        """
        Candidate erasure (review item 13) - the stage rows and stage-failure rows of these tasks,
        whatever their document_id. A parse that failed never linked its rows to the resume
        (link_document_id runs only on success), and its rows can quote the resume (str(exc)), so
        erasure finds them by the task ids of the candidate's own parses. A task parses one document.
        Returns the stage rows deleted.
        """
        task_ids = [t for t in task_ids or () if t]
        if not task_ids:
            return 0
        deleted = self.db.execute(delete(DocumentProcessingStageExecution).where(
            DocumentProcessingStageExecution.task_id.in_(task_ids))).rowcount or 0
        self.db.execute(delete(StageFailureLog).where(StageFailureLog.task_id.in_(task_ids)))
        self.db.flush()
        return deleted

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
