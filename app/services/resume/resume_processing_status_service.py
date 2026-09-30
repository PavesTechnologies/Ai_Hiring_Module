from uuid import UUID

from app.exception_handler.exceptions import NotFoundError
from app.models.async_tasks import DocumentType
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.document_processing_repository import DocumentProcessingRepository
from app.schemas.resume.response import ResumeProcessingStatusResponse, StageProgress
from app.services.document_processing.error_presenter import to_user_message
from app.services.document_processing.retry_policy import get_max_attempts
from app.services.document_processing.stage_summary import (
    collapse_to_latest_attempt,
    retry_budget,
    stage_retries_remaining,
)
from app.websocket.events import WebSocketEvent, WebSocketEventType


class ResumeProcessingStatusService:
    """
    Looks up the status of an in-flight or completed resume processing task
    for the polling endpoint, combining the task-level status
    (CeleryTaskLog) with per-stage detail
    (DocumentProcessingStageExecution) — structural mirror of
    JDProcessingStatusService, kept as its own class rather than shared
    (same rationale as ResumeProcessingContext vs. JDProcessingContext).
    The two stateless calculations both need — collapsing attempts and
    deriving the retry budget — live in stage_summary rather than being
    copied here, so JD and Resume can never report different progress for
    identical rows.
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
                error_message=to_user_message(execution.error_message),
                error_detail=execution.error_message,
                duration_ms=execution.duration_ms,
                attempt_number=execution.attempt_number,
                max_attempts=get_max_attempts(execution.stage),
                retries_remaining=stage_retries_remaining(execution),
            )
            for execution in executions
        ]

    def get_status(self, task_id: UUID, include_attempts: bool = False) -> ResumeProcessingStatusResponse:
        task_log = self.task_log_repository.get_by_task_id(str(task_id))
        if not task_log:
            raise NotFoundError(f"No processing task found for task_id {task_id}.")

        executions = self.stage_repository.get_by_task_id(str(task_id))
        collapsed = collapse_to_latest_attempt(executions)
        stages = self._build_stages(executions if include_attempts else collapsed)
        max_attempts, retries_remaining = retry_budget(task_log, collapsed)

        return ResumeProcessingStatusResponse(
            task_id=task_id,
            overall_status=task_log.status.value,
            # Always from the collapsed list: its last element is the
            # furthest stage in STAGE_ORDER the task actually reached, so
            # current_stage means the same thing whether or not the caller
            # asked for the full attempt history.
            current_stage=collapsed[-1].stage.value if collapsed else None,
            stages=stages,
            resume_id=task_log.resume_id,
            error_message=to_user_message(task_log.error_message),
            error_detail=task_log.error_message,
            retry_count=task_log.retry_count or 0,
            max_attempts=max_attempts,
            retries_remaining=retries_remaining,
        )

    def get_status_as_events(self, task_id: str) -> list[WebSocketEvent]:
        """
        Bug fix: a client that opens the /resumes/processing-status/{task_id}
        WebSocket after the pipeline has already progressed - or fully
        finished - never sees any of that history. WebSocketEvent delivery
        is plain Redis Pub/Sub (see publish_event): a message published
        before a subscriber joins that channel is gone, not queued. A
        resume pipeline can clear some stages in well under a second and
        the whole run in well under a minute (routinely faster than the
        time it takes a client to receive its upload response and open a
        fresh socket for the task_id it just got back), so the client is
        left rendering its very first ("about to start") render forever,
        even though the task reached SUCCESS/FAILURE long ago and no
        further event will ever be published on this channel again.

        Called once, immediately after a client connects, to replay every
        task.linked/stage.completed event it would have received had it
        been subscribed from the very start - identical wire shape to
        publish_task_linked/publish_stage_completed, so the existing
        frontend event handler needs no changes to consume this catch-up
        burst before the live relay continues normally.
        """
        task_log = self.task_log_repository.get_by_task_id(str(task_id))
        if not task_log:
            return []

        events: list[WebSocketEvent] = []

        if task_log.resume_id is not None:
            events.append(
                WebSocketEvent(
                    event=WebSocketEventType.TASK_LINKED,
                    data={
                        "task_id": str(task_id),
                        "document_type": DocumentType.RESUME.value,
                        "document_id": str(task_log.resume_id),
                    },
                )
            )

        executions = self.stage_repository.get_by_task_id(str(task_id))
        collapsed = collapse_to_latest_attempt(executions)
        for execution in collapsed:
            events.append(
                WebSocketEvent(
                    event=WebSocketEventType.STAGE_COMPLETED,
                    data={
                        "task_id": str(task_id),
                        "document_type": DocumentType.RESUME.value,
                        "stage": execution.stage.value,
                        "status": execution.status.value,
                        "error_message": execution.error_message,
                        "duration_ms": execution.duration_ms,
                        "attempt_number": execution.attempt_number,
                        "max_attempts": get_max_attempts(execution.stage),
                        "retries_remaining": stage_retries_remaining(execution),
                    },
                )
            )

        return events
