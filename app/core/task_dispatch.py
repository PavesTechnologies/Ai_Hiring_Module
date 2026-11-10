"""
Is a celery_task_log row really in flight, and what happens when a dispatch fails (review item 8).

Every enqueue helper commits a QUEUED row, then hands the task to the broker. Before this:
- a failed hand-off only logged, leaving the row QUEUED for good;
- every later check treated QUEUED/RUNNING/RETRY as in flight with no age limit,
so one lost message stranded the candidate: nothing re-queued it and nothing retried it.

Now:
- dispatch_or_mark_lost: a hand-off that raises marks the row lost (FAILURE, dispatch_failed=True),
  so it is terminal and visible, and the next trigger or recovery scan queues it again;
- is_in_flight: QUEUED/RUNNING/RETRY counts as in flight only while the row was touched within
  IN_FLIGHT_STALE_AFTER - longer than any task may legitimately be held (the longest retry countdown
  plus its run), so an older row's message is gone and the row no longer blocks a re-queue;
- the 5-minute stalled-task sweep (resume_processing_tasks.recover_stalled_resume_uploads) marks
  stale rows of every task type lost once no worker holds them.

"Lost" (FAILURE with dispatch_failed=True) means the work never ran to completion and is safe to redo:
never dispatched, or stalled with no worker holding it. A plain FAILURE is a real failure, which some
task types (resume embedding) deliberately don't redo on their own.
"""
import logging
from datetime import datetime, timedelta, timezone

from app.models.async_tasks import CeleryTaskLog, TaskStatus

logger = logging.getLogger(__name__)

IN_FLIGHT_STATUSES = (TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.RETRY)

# Longer than any task is legitimately held: today the longest retry countdown is 480 s (the decision-
# pending and v2 policies) plus a run well under 10 minutes.
IN_FLIGHT_STALE_AFTER = timedelta(minutes=45)

LOST_DISPATCH_MESSAGE = "Lost: handing the task to the queue failed ({error}). Safe to queue again."
LOST_STALLED_MESSAGE = ("Lost: no worker held this task and it was untouched for over {minutes} minutes. "
                        "Safe to queue again.")


def last_touched(log: CeleryTaskLog) -> datetime | None:
    return getattr(log, "started_at", None) or getattr(log, "queued_at", None)


def is_in_flight(log: CeleryTaskLog, now: datetime | None = None) -> bool:
    """Queued, running or waiting to retry - and recent enough that its message can still be live."""
    if getattr(log, "status", None) not in IN_FLIGHT_STATUSES:
        return False
    touched = last_touched(log)
    if not isinstance(touched, datetime):
        return True     # no timestamp to judge by: assume live, as before
    now = now or datetime.now(timezone.utc)
    return now - touched < IN_FLIGHT_STALE_AFTER


def is_lost(log: CeleryTaskLog) -> bool:
    return getattr(log, "status", None) == TaskStatus.FAILURE and getattr(log, "dispatch_failed", False) is True


def mark_lost(log: CeleryTaskLog, task_log_service, message: str) -> None:
    """FAILURE + dispatch_failed, committed. Never raises: it is bookkeeping after a failure."""
    try:
        log.status = TaskStatus.FAILURE
        log.dispatch_failed = True
        log.error_message = message
        log.completed_at = datetime.now(timezone.utc)
        task_log_service.repository.update(log)
        task_log_service.repository.commit()
    except Exception:
        logger.exception("Could not mark task %s lost", getattr(log, "task_id", None))
        try:
            task_log_service.repository.rollback()
        except Exception:
            logger.exception("Rollback after marking task %s lost failed", getattr(log, "task_id", None))


def dispatch_or_mark_lost(task, *, kwargs: dict, log: CeleryTaskLog, task_log_service) -> bool:
    """apply_async under the row's task_id; on failure the row is marked lost. True when handed over."""
    try:
        task.apply_async(kwargs=kwargs, task_id=log.task_id)
        return True
    except Exception as exc:
        logger.exception("Dispatch failed | task_type=%s task_id=%s",
                         getattr(log, "task_type", None), getattr(log, "task_id", None))
        mark_lost(log, task_log_service, LOST_DISPATCH_MESSAGE.format(error=type(exc).__name__))
        return False


def reset_for_redispatch(log: CeleryTaskLog, task_id: str) -> None:
    """Reuse a lost or stale row for a fresh attempt (the caller commits)."""
    log.task_id = task_id
    log.status = TaskStatus.QUEUED
    log.dispatch_failed = False
    log.error_message = None
    log.queued_at = datetime.now(timezone.utc)
    log.started_at = None
    log.completed_at = None
