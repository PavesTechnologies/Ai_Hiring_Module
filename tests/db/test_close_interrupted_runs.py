"""Closing a periodic task's runs that never finished, against real Postgres (rolled back)."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.models.async_tasks import CeleryTaskLog, TaskStatus
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from tests.db.test_campaign_candidate_delete_scored import db  # noqa: F401 - fixture

TYPE = "ZZ_TEST_PERIODIC_SCAN"


def _log(db, status, minutes_ago):  # noqa: F811
    row = CeleryTaskLog(task_id=f"zz-{uuid4()}", task_type=TYPE, status=status,
                        queued_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))
    db.add(row)
    return row


def test_only_old_unfinished_runs_of_that_task_type_are_closed(db):  # noqa: F811
    old_queued, old_running = _log(db, TaskStatus.QUEUED, 600), _log(db, TaskStatus.RUNNING, 90)
    recent, finished, current = _log(db, TaskStatus.QUEUED, 2), _log(db, TaskStatus.SUCCESS, 600), \
        _log(db, TaskStatus.RUNNING, 999)
    db.flush()

    closed = CeleryTaskLogRepository(db).close_interrupted_runs(
        TYPE, started_before=datetime.now(timezone.utc) - timedelta(minutes=30), exclude_task_id=current.task_id,
        message="interrupted")
    for row in (old_queued, old_running, recent, finished, current):
        db.refresh(row)

    assert closed == 2
    assert (old_queued.status, old_running.status) == (TaskStatus.FAILURE, TaskStatus.FAILURE)
    assert old_queued.error_message == "interrupted" and old_queued.completed_at is not None
    assert (recent.status, finished.status, current.status) == (TaskStatus.QUEUED, TaskStatus.SUCCESS,
                                                                TaskStatus.RUNNING)
