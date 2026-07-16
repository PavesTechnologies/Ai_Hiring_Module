from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func
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
