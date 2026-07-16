from uuid import UUID

from app.exception_handler.exceptions import NotFoundError
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.document_processing_repository import DocumentProcessingRepository
from app.schemas.resume.response import ResumeProcessingStatusResponse, StageProgress


class ResumeProcessingStatusService:
    """
    Looks up the status of an in-flight or completed resume processing task
    for the polling endpoint, combining the task-level status
    (CeleryTaskLog) with per-stage detail
    (DocumentProcessingStageExecution) — structural mirror of
    JDProcessingStatusService, kept as its own class rather than shared
    (same rationale as ResumeProcessingContext vs. JDProcessingContext).
    """

    def __init__(
        self,
        task_log_repository: CeleryTaskLogRepository,
        stage_repository: DocumentProcessingRepository,
    ):
        self.task_log_repository = task_log_repository
        self.stage_repository = stage_repository

    def get_status(self, task_id: UUID) -> ResumeProcessingStatusResponse:
        task_log = self.task_log_repository.get_by_task_id(str(task_id))
        if not task_log:
            raise NotFoundError(f"No processing task found for task_id {task_id}.")

        executions = self.stage_repository.get_by_task_id(str(task_id))
        stages = [
            StageProgress(
                stage=execution.stage.value,
                status=execution.status.value,
                error_message=execution.error_message,
                duration_ms=execution.duration_ms,
            )
            for execution in executions
        ]

        return ResumeProcessingStatusResponse(
            task_id=task_id,
            overall_status=task_log.status.value,
            current_stage=stages[-1].stage if stages else None,
            stages=stages,
            resume_id=task_log.resume_id,
            error_message=task_log.error_message,
        )
