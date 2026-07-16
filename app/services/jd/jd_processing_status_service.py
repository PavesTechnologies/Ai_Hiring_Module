from uuid import UUID

from app.exception_handler.exceptions import NotFoundError
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.document_processing_repository import DocumentProcessingRepository
from app.schemas.jd.response import JDProcessingStatusResponse, JDUploadSummary, StageProgress


class JDProcessingStatusService:
    """
    Looks up the status of an in-flight or completed JD processing task for
    the polling endpoint, combining the task-level status (CeleryTaskLog)
    with per-stage detail (DocumentProcessingStageExecution).
    """

    def __init__(
        self,
        task_log_repository: CeleryTaskLogRepository,
        stage_repository: DocumentProcessingRepository,
    ):
        self.task_log_repository = task_log_repository
        self.stage_repository = stage_repository

    @staticmethod
    def _build_stages(executions) -> list[StageProgress]:
        return [
            StageProgress(
                stage=execution.stage.value,
                status=execution.status.value,
                error_message=execution.error_message,
                duration_ms=execution.duration_ms,
            )
            for execution in executions
        ]

    def get_status(self, task_id: UUID) -> JDProcessingStatusResponse:
        task_log = self.task_log_repository.get_by_task_id(str(task_id))
        if not task_log:
            raise NotFoundError(f"No processing task found for task_id {task_id}.")

        stages = self._build_stages(self.stage_repository.get_by_task_id(str(task_id)))

        return JDProcessingStatusResponse(
            task_id=task_id,
            overall_status=task_log.status.value,
            current_stage=stages[-1].stage if stages else None,
            stages=stages,
            jd_id=task_log.jd_id,
            error_message=task_log.error_message,
        )

    def get_recent_uploads(self, created_by: str, limit: int = 50) -> list[JDUploadSummary]:
        """
        "My uploads" list: every JD create/reprocess task a user has
        submitted, newest first, each with its full per-stage breakdown —
        so a user knows not just whether an upload succeeded/failed but
        exactly which stage it's at or died in, without having to look up
        each task_id individually via get_status().
        """
        task_logs = self.task_log_repository.get_recent_by_created_by(created_by, limit)
        task_ids = [log.task_id for log in task_logs]

        executions_by_task: dict[str, list] = {}
        for execution in self.stage_repository.get_by_task_ids(task_ids):
            executions_by_task.setdefault(execution.task_id, []).append(execution)

        summaries = []
        for log in task_logs:
            stages = self._build_stages(executions_by_task.get(log.task_id, []))
            summaries.append(
                JDUploadSummary(
                    task_id=UUID(log.task_id),
                    title=log.title,
                    status=log.status.value,
                    current_stage=stages[-1].stage if stages else None,
                    stages=stages,
                    jd_id=log.jd_id,
                    error_message=log.error_message,
                    queued_at=log.queued_at,
                )
            )
        return summaries
