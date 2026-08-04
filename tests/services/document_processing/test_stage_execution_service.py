import pytest
from unittest.mock import MagicMock

from app.models.async_tasks import DocumentType, ProcessingStage, StageExecutionStatus
from app.services.document_processing.stage_execution_service import (
    StageExecutionError,
    StageExecutionService,
)

"""
Covers the gap where a DB failure in start_stage/complete_stage's own
commit (e.g. connection-slot exhaustion) used to propagate as a raw
exception, bypassing StageExecutionError/RetryDriver entirely and
permanently failing the task with no retry.
"""


def _service_with(repository):
    return StageExecutionService(repository)


def test_start_stage_commit_failure_raises_stage_execution_error():
    repository = MagicMock()
    repository.start_stage.return_value = MagicMock()
    repository.commit.side_effect = ConnectionError("connection to server failed")
    service = _service_with(repository)

    with pytest.raises(StageExecutionError) as exc_info:
        service.run_stage("task-1", DocumentType.JD, ProcessingStage.AI_EXTRACTION, fn=MagicMock())

    assert exc_info.value.stage == ProcessingStage.AI_EXTRACTION
    assert isinstance(exc_info.value.original, ConnectionError)


def test_complete_stage_commit_failure_after_successful_fn_raises_stage_execution_error():
    repository = MagicMock()
    repository.start_stage.return_value = MagicMock()
    # First commit (start_stage) succeeds, second (complete_stage) fails.
    repository.commit.side_effect = [None, ConnectionError("connection to server failed")]
    service = _service_with(repository)

    with pytest.raises(StageExecutionError) as exc_info:
        service.run_stage(
            "task-1", DocumentType.JD, ProcessingStage.AI_EXTRACTION, fn=MagicMock(return_value="ok")
        )

    assert isinstance(exc_info.value.original, ConnectionError)


def test_fn_failure_still_raises_original_exception_when_bookkeeping_also_fails():
    repository = MagicMock()
    repository.start_stage.return_value = MagicMock()
    repository.commit.side_effect = [None, ConnectionError("connection to server failed")]
    service = _service_with(repository)

    def failing_fn():
        raise ValueError("business logic failure")

    with pytest.raises(StageExecutionError) as exc_info:
        service.run_stage("task-1", DocumentType.JD, ProcessingStage.AI_EXTRACTION, fn=failing_fn)

    # The original business failure must win, not the secondary bookkeeping failure.
    assert isinstance(exc_info.value.original, ValueError)


def test_successful_stage_still_completes_normally():
    repository = MagicMock()
    execution = MagicMock()
    repository.start_stage.return_value = execution
    repository.complete_stage.return_value = execution
    service = _service_with(repository)

    result = service.run_stage(
        "task-1", DocumentType.JD, ProcessingStage.AI_EXTRACTION, fn=MagicMock(return_value="ok")
    )

    assert result == "ok"
    repository.complete_stage.assert_called_once()
    assert repository.complete_stage.call_args[0][1] == StageExecutionStatus.SUCCESS
