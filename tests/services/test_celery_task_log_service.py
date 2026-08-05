import json
from unittest.mock import MagicMock

from app.models.async_tasks import CeleryTaskLog, TaskStatus
from app.services.celery_task_log_service import CeleryTaskLogService


def _make_service():
    repo = MagicMock()
    repo.update.side_effect = lambda log: log
    return CeleryTaskLogService(repo), repo


def test_mark_dispatch_failed_keeps_status_queued():
    """
    Resume-upload resilience: dispatch_failed is not a terminal failure
    (unlike mark_failure/mark_dead) - status must stay QUEUED so the
    recovery job's get_queued_dispatch_failed query still finds it.
    """
    service, repo = _make_service()
    log = CeleryTaskLog(task_id="t1", task_type="RESUME_DOCUMENT_PROCESSING", status=TaskStatus.QUEUED)

    result = service.mark_dispatch_failed(log, "broker unreachable")

    assert result.status == TaskStatus.QUEUED
    assert result.dispatch_failed is True
    assert result.error_message == "broker unreachable"
    assert json.loads(result.output_summary) == {"enqueue_failed": True, "reason": "broker unreachable"}
    repo.commit.assert_called_once()
