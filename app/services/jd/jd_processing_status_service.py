from datetime import datetime, timedelta, timezone
from uuid import UUID

from app.exception_handler.exceptions import NotFoundError
from app.models.async_tasks import DocumentType, TaskStatus
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.document_processing_repository import DocumentProcessingRepository
from app.schemas.jd.response import (
    JDProcessingStatusResponse,
    JDUploadStageProgress,
    JDUploadSummary,
    StageProgress,
)
from app.services.document_processing.error_presenter import to_user_message
from app.services.document_processing.retry_policy import get_max_attempts
from app.services.document_processing.stage_summary import (
    collapse_to_latest_attempt,
    retry_budget,
    stage_retries_remaining,
)
from app.websocket.events import WebSocketEvent, WebSocketEventType

_IN_FLIGHT_STATUSES = {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.RETRY}
_SNAPSHOT_RECENT_WINDOW = timedelta(minutes=10)


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
    def _build_history_stages(executions) -> list[JDUploadStageProgress]:
        """Upload history: raw rows, raw error text — unchanged behaviour."""
        return [
            JDUploadStageProgress(
                stage=execution.stage.value,
                status=execution.status.value,
                error_message=execution.error_message,
                duration_ms=execution.duration_ms,
            )
            for execution in executions
        ]

    @staticmethod
    def _build_stages(executions) -> list[StageProgress]:
        return [
            StageProgress(
                stage=execution.stage.value,
                status=execution.status.value,
                error_message=to_user_message(execution.error_message),
                error_detail=execution.error_message,
                duration_ms=execution.duration_ms,
                attempt_number=execution.attempt_number,
                max_attempts=get_max_attempts(execution.stage),
                retries_remaining=stage_retries_remaining(execution),
            )
            for execution in executions
        ]


    def get_status(self, task_id: UUID, include_attempts: bool = False) -> JDProcessingStatusResponse:
        task_log = self.task_log_repository.get_by_task_id(str(task_id))
        if not task_log:
            raise NotFoundError(f"No processing task found for task_id {task_id}.")

        executions = self.stage_repository.get_by_task_id(str(task_id))
        collapsed = collapse_to_latest_attempt(executions)
        stages = self._build_stages(executions if include_attempts else collapsed)
        max_attempts, retries_remaining = retry_budget(task_log, collapsed)

        return JDProcessingStatusResponse(
            task_id=task_id,
            overall_status=task_log.status.value,
            # Always from the collapsed list: its last element is the
            # furthest stage in STAGE_ORDER the task actually reached, so
            # current_stage means the same thing whether or not the caller
            # asked for the full attempt history.
            current_stage=collapsed[-1].stage.value if collapsed else None,
            stages=stages,
            jd_id=task_log.jd_id,
            retry_count=task_log.retry_count or 0,
            max_attempts=max_attempts,
            retries_remaining=retries_remaining,
            error_message=to_user_message(task_log.error_message),
            error_detail=task_log.error_message,
        )

    def get_recent_uploads_as_events(self, created_by: str, limit: int = 50) -> list[WebSocketEvent]:
        """
        Catch-up burst for the per-user JD WebSocket on connect - same wire
        shape as the live task.linked/stage.completed events, same reason as
        ResumeProcessingStatusService.get_status_as_events (Pub/Sub has no
        backlog). Covers the user's uploads still in flight or finished in
        the last few minutes; older ones are already on the REST list.
        """
        recent_cutoff = datetime.now(timezone.utc) - _SNAPSHOT_RECENT_WINDOW
        task_logs = [
            log for log in self.task_log_repository.get_recent_by_created_by(created_by, limit)
            if log.status in _IN_FLIGHT_STATUSES or (log.completed_at and log.completed_at >= recent_cutoff)
        ]
        if not task_logs:
            return []

        executions_by_task: dict[str, list] = {}
        for execution in self.stage_repository.get_by_task_ids([log.task_id for log in task_logs]):
            executions_by_task.setdefault(execution.task_id, []).append(execution)

        events: list[WebSocketEvent] = []
        for log in task_logs:
            if log.jd_id is not None:
                events.append(WebSocketEvent(
                    event=WebSocketEventType.TASK_LINKED,
                    data={"task_id": log.task_id, "document_type": DocumentType.JD.value, "document_id": str(log.jd_id)},
                ))
            for execution in collapse_to_latest_attempt(executions_by_task.get(log.task_id, [])):
                events.append(WebSocketEvent(
                    event=WebSocketEventType.STAGE_COMPLETED,
                    data={
                        "task_id": log.task_id,
                        "document_type": DocumentType.JD.value,
                        "stage": execution.stage.value,
                        "status": execution.status.value,
                        "error_message": execution.error_message,
                        "duration_ms": execution.duration_ms,
                        "attempt_number": execution.attempt_number,
                        "max_attempts": get_max_attempts(execution.stage),
                        "retries_remaining": stage_retries_remaining(execution),
                    },
                ))
        return events

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
            # Upload history renders the raw attempt-by-attempt list and the
            # raw error string, exactly as it always has. Stage collapsing
            # and the friendly-error mapping are deliberately confined to
            # the processing view (get_status) so this tab's existing UI is
            # untouched.
            stages = self._build_history_stages(executions_by_task.get(log.task_id, []))
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
