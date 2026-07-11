import time
from typing import Callable, TypeVar
from uuid import UUID

from app.models.async_tasks import (
    DocumentProcessingStageExecution,
    DocumentType,
    ProcessingStage,
    StageExecutionStatus,
)
from app.repositories.document_processing_repository import DocumentProcessingRepository

T = TypeVar("T")


class StageExecutionService:
    """
    Records per-stage progress for an async document-processing pipeline run.
    Document-type-agnostic (JD today, Resume later) — mirrors the
    create_log/mark_success/mark_failure shape of CeleryTaskLogService.
    """

    def __init__(self, repository: DocumentProcessingRepository):
        self.repository = repository

    def start_stage(
        self,
        task_id: str,
        document_type: DocumentType,
        stage: ProcessingStage,
        attempt_number: int = 1,
    ) -> DocumentProcessingStageExecution:
        execution = self.repository.start_stage(task_id, document_type, stage, attempt_number)
        self.repository.commit()
        return execution

    def complete_stage(
        self,
        execution: DocumentProcessingStageExecution,
        status: StageExecutionStatus,
        error_message: str | None = None,
        duration_ms: int | None = None,
    ) -> DocumentProcessingStageExecution:
        execution = self.repository.complete_stage(execution, status, error_message, duration_ms)
        self.repository.commit()
        return execution

    def skip_stage(
        self,
        task_id: str,
        document_type: DocumentType,
        stage: ProcessingStage,
        attempt_number: int = 1,
    ) -> None:
        execution = self.repository.start_stage(task_id, document_type, stage, attempt_number)
        self.repository.complete_stage(execution, StageExecutionStatus.SKIPPED)
        self.repository.commit()

    def run_stage(
        self,
        task_id: str,
        document_type: DocumentType,
        stage: ProcessingStage,
        fn: Callable[[], T],
        attempt_number: int = 1,
    ) -> T:
        execution = self.start_stage(task_id, document_type, stage, attempt_number)
        started = time.monotonic()
        try:
            result = fn()
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            self.complete_stage(execution, StageExecutionStatus.FAILED, str(exc), duration_ms)
            raise
        duration_ms = int((time.monotonic() - started) * 1000)
        self.complete_stage(execution, StageExecutionStatus.SUCCESS, duration_ms=duration_ms)
        return result

    def next_attempt_number(self, task_id: str, stage: ProcessingStage) -> int:
        """
        Retry hook: the attempt_number a future retry of this stage should
        use. Not invoked by run_stage today — automatic retries aren't
        implemented yet — but available so a retry driver can be added
        later without changing this service's shape or the tracking schema.
        """
        return self.repository.get_latest_attempt_number(task_id, stage) + 1

    def link_document_id(self, task_id: str, document_id: UUID) -> None:
        self.repository.link_document_id(task_id, document_id)
        self.repository.commit()
