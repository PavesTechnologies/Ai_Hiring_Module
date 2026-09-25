from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.models.async_tasks import DocumentType, ProcessingStage, StageExecutionStatus, TaskStatus
from app.services.jd.jd_processing_status_service import JDProcessingStatusService
from app.websocket.events import WebSocketEventType


def _log(task_id, status, completed_minutes_ago=None, jd_id=None):
    completed_at = (
        datetime.now(timezone.utc) - timedelta(minutes=completed_minutes_ago)
        if completed_minutes_ago is not None else None
    )
    return SimpleNamespace(task_id=task_id, status=status, completed_at=completed_at, jd_id=jd_id)


def _execution(task_id, stage, status):
    return SimpleNamespace(
        task_id=task_id, stage=stage, status=status, attempt_number=1,
        error_message=None, duration_ms=10,
    )


def _service(logs, executions):
    task_log_repository = MagicMock()
    task_log_repository.get_recent_by_created_by.return_value = logs
    stage_repository = MagicMock()
    stage_repository.get_by_task_ids.return_value = executions
    return JDProcessingStatusService(task_log_repository, stage_repository), stage_repository


def test_replays_in_flight_and_recently_failed_uploads_only():
    running = _log("t-running", TaskStatus.RUNNING)
    fresh_failure = _log("t-failed", TaskStatus.FAILURE, completed_minutes_ago=2, jd_id=uuid4())
    old_failure = _log("t-old", TaskStatus.FAILURE, completed_minutes_ago=60)
    executions = [
        _execution("t-running", ProcessingStage.AI_EXTRACTION, StageExecutionStatus.SUCCESS),
        _execution("t-failed", ProcessingStage.AI_EXTRACTION, StageExecutionStatus.FAILED),
    ]
    service, stage_repository = _service([running, fresh_failure, old_failure], executions)

    events = service.get_recent_uploads_as_events("user-1")

    stage_repository.get_by_task_ids.assert_called_once_with(["t-running", "t-failed"])
    stage_events = [e for e in events if e.event == WebSocketEventType.STAGE_COMPLETED]
    assert {e.data["task_id"] for e in stage_events} == {"t-running", "t-failed"}
    assert all(e.data["document_type"] == DocumentType.JD.value for e in events)
    linked = [e for e in events if e.event == WebSocketEventType.TASK_LINKED]
    assert [e.data["document_id"] for e in linked] == [str(fresh_failure.jd_id)]


def test_no_recent_uploads_means_no_events_and_no_stage_query():
    service, stage_repository = _service([_log("t-old", TaskStatus.FAILURE, completed_minutes_ago=60)], [])

    assert service.get_recent_uploads_as_events("user-1") == []
    stage_repository.get_by_task_ids.assert_not_called()
