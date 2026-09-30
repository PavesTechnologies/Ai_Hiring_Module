from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.models.async_tasks import DocumentType, ProcessingStage, StageExecutionStatus, TaskStatus
from app.services.resume.resume_processing_status_service import ResumeProcessingStatusService
from app.websocket.events import WebSocketEventType

"""
ResumeProcessingStatusService.get_status_as_events - bug fix.

A client connecting to /resumes/processing-status/{task_id} only after the
pipeline has already progressed (or fully finished) used to see nothing:
WebSocketEvent delivery is plain Redis Pub/Sub with no backlog, so every
task.linked/stage.completed event published before that client subscribed
is gone for good. A resume pipeline routinely finishes well under a minute
- faster than a client can receive its upload response and open a fresh
socket for the task_id it just got back - so the "In Processing" card was
left rendering "queued, 0%" forever even though the task had long since
reached SUCCESS/FAILURE. get_status_as_events replays that missed history
in the exact wire shape the live events already use, so a client that
connects late catches up immediately.
"""


def _make_execution(stage, status=StageExecutionStatus.SUCCESS, attempt_number=1, duration_ms=100, error_message=None):
    return SimpleNamespace(
        stage=stage, status=status, attempt_number=attempt_number,
        duration_ms=duration_ms, error_message=error_message,
    )


def _make_service(task_log, executions):
    task_log_repository = MagicMock()
    task_log_repository.get_by_task_id.return_value = task_log
    stage_repository = MagicMock()
    stage_repository.get_by_task_id.return_value = executions
    return ResumeProcessingStatusService(task_log_repository, stage_repository)


def test_no_task_log_for_task_id_returns_no_events():
    """Unknown/garbage task_id - best-effort snapshot, never raises (unlike get_status's NotFoundError)."""
    service = _make_service(task_log=None, executions=[])

    events = service.get_status_as_events(str(uuid4()))

    assert events == []


def test_replays_task_linked_first_when_resume_id_is_known():
    resume_id = uuid4()
    task_log = SimpleNamespace(resume_id=resume_id, retry_count=0, status=TaskStatus.RUNNING, error_message=None)
    service = _make_service(task_log, executions=[])

    events = service.get_status_as_events("task-123")

    assert len(events) == 1
    assert events[0].event == WebSocketEventType.TASK_LINKED
    assert events[0].data["document_id"] == str(resume_id)
    assert events[0].data["document_type"] == DocumentType.RESUME.value


def test_no_task_linked_event_when_resume_id_not_yet_set():
    """Task created but not yet linked to a resume - matches publish_task_linked's own precondition."""
    task_log = SimpleNamespace(resume_id=None, retry_count=0, status=TaskStatus.RUNNING, error_message=None)
    service = _make_service(task_log, executions=[])

    events = service.get_status_as_events("task-123")

    assert events == []


def test_replays_one_stage_completed_event_per_collapsed_stage_in_pipeline_order():
    resume_id = uuid4()
    task_log = SimpleNamespace(resume_id=resume_id, retry_count=0, status=TaskStatus.SUCCESS, error_message=None)
    executions = [
        _make_execution(ProcessingStage.PERSISTENCE),
        _make_execution(ProcessingStage.TEXT_EXTRACTION),
        _make_execution(ProcessingStage.AI_EXTRACTION),
    ]
    service = _make_service(task_log, executions)

    events = service.get_status_as_events("task-123")

    stage_events = [e for e in events if e.event == WebSocketEventType.STAGE_COMPLETED]
    assert [e.data["stage"] for e in stage_events] == [
        ProcessingStage.TEXT_EXTRACTION.value,
        ProcessingStage.AI_EXTRACTION.value,
        ProcessingStage.PERSISTENCE.value,
    ]


def test_stage_completed_event_carries_the_same_fields_as_the_live_publisher():
    resume_id = uuid4()
    task_log = SimpleNamespace(resume_id=resume_id, retry_count=0, status=TaskStatus.SUCCESS, error_message=None)
    execution = _make_execution(ProcessingStage.AI_EXTRACTION, duration_ms=29297, attempt_number=1)
    service = _make_service(task_log, [execution])

    events = service.get_status_as_events("task-123")
    stage_event = next(e for e in events if e.event == WebSocketEventType.STAGE_COMPLETED)

    assert stage_event.data == {
        "task_id": "task-123",
        "document_type": DocumentType.RESUME.value,
        "stage": ProcessingStage.AI_EXTRACTION.value,
        "status": StageExecutionStatus.SUCCESS.value,
        "error_message": None,
        "duration_ms": 29297,
        "attempt_number": 1,
        "max_attempts": 5,  # AI_EXTRACTION's own larger budget (STAGE_POLICIES)
        "retries_remaining": None,  # only non-null on a FAILED row
    }


def test_a_fully_finished_task_replays_every_stage_so_a_late_client_sees_the_terminal_state():
    """The exact scenario that was silently dropped before this fix - a task that reached SUCCESS before the client ever subscribed."""
    resume_id = uuid4()
    task_log = SimpleNamespace(resume_id=resume_id, retry_count=0, status=TaskStatus.SUCCESS, error_message=None)
    executions = [_make_execution(stage) for stage in [
        ProcessingStage.TEXT_EXTRACTION, ProcessingStage.TEXT_CLEANING, ProcessingStage.PII_DETECTION,
        ProcessingStage.PII_REDACTION, ProcessingStage.AI_EXTRACTION, ProcessingStage.JSON_VALIDATION,
        ProcessingStage.SKILL_NORMALIZATION, ProcessingStage.PERSISTENCE,
    ]]
    service = _make_service(task_log, executions)

    events = service.get_status_as_events("task-123")

    # 1 task.linked + 8 stage.completed
    assert len(events) == 9
    assert events[0].event == WebSocketEventType.TASK_LINKED
    assert events[-1].data["stage"] == ProcessingStage.PERSISTENCE.value
    assert all(e.data["status"] == StageExecutionStatus.SUCCESS.value for e in events[1:])
