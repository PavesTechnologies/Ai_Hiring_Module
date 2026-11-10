"""
Erasure file cleanup (review item 14).

Candidate erasure commits the database deletion first and then deletes the stored files. A file whose
delete fails is handed to delete_erased_files, which retries with backoff; one still failing after the
last attempt is dead-lettered with its paths (task type ERASURE_FILE_DELETE), so it shows as an open
dead letter in monitoring for a person to finish. The paths are kept only so the files can be deleted.
"""
import logging
from datetime import datetime, timezone
from uuid import uuid4

from celery.exceptions import Retry

from app.core.celery_app import bounded_countdown, celery_app
from app.core.storage_service import StorageService
from app.core.task_dispatch import dispatch_or_mark_lost
from app.db.session import SessionLocal, rollback_quietly
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.dead_letter_queue_repository import DeadLetterQueueRepository
from app.services.celery_task_log_service import CeleryTaskLogService

logger = logging.getLogger(__name__)

ERASURE_FILE_DELETE_TASK_TYPE = "ERASURE_FILE_DELETE"
MAX_ATTEMPTS = 6                     # about 1 + 2 + 4 + 8 + 16 minutes of retries
_BASE_DELAY_SECONDS = 60


def _delete_all(storage, bucket: str, paths: list[str]) -> list[str]:
    """Deletes each file; returns the paths that still failed."""
    failed = []
    for path in paths:
        try:
            storage.delete_file(bucket, path)
        except Exception as exc:
            logger.warning("Erasure file delete failed (%s) - %s", type(exc).__name__, "will retry or dead-letter")
            failed.append(path)
    return failed


def dead_letter_paths(db, *, task_id: str, bucket: str, paths: list[str], attempts: int, first_at: datetime,
                      reason: str) -> None:
    """Records the files still to delete as an open dead letter (committed)."""
    repo = DeadLetterQueueRepository(db)
    repo.create(original_task_id=task_id, task_type=ERASURE_FILE_DELETE_TASK_TYPE,
                final_error_message=f"{len(paths)} erased candidate file(s) not deleted: {reason}",
                input_payload={"bucket": bucket, "paths": list(paths)}, retry_count=attempts,
                first_attempted_at=first_at, last_attempted_at=datetime.now(timezone.utc))
    repo.commit()


def queue_erased_file_deletes(db, bucket: str, paths: list[str]) -> str | None:
    """
    Logs and dispatches the retry for these paths; returns its task id. If the hand-off fails, the paths
    are dead-lettered at once, so they are always recorded somewhere. Never raises.
    """
    if not paths:
        return None
    task_log_service = CeleryTaskLogService(CeleryTaskLogRepository(db))
    try:
        log = task_log_service.create_log(task_id=str(uuid4()), task_type=ERASURE_FILE_DELETE_TASK_TYPE)
        if not dispatch_or_mark_lost(delete_erased_files, kwargs={"bucket": bucket, "paths": list(paths)},
                                     log=log, task_log_service=task_log_service):
            dead_letter_paths(db, task_id=log.task_id, bucket=bucket, paths=paths, attempts=0,
                              first_at=datetime.now(timezone.utc), reason="queueing the retry failed")
        return log.task_id
    except Exception:
        logger.exception("Could not record the erased-file retry for %s file(s)", len(paths))
        rollback_quietly(db)
        return None


@celery_app.task(bind=True, name="compliance.delete_erased_files", max_retries=MAX_ATTEMPTS - 1)
def delete_erased_files(self, bucket: str, paths: list[str]) -> None:
    db = SessionLocal()
    task_log_service = CeleryTaskLogService(CeleryTaskLogRepository(db))
    task_log = None
    try:
        task_log = task_log_service.repository.get_by_task_id(self.request.id)
        if task_log is not None:
            task_log_service.mark_running(task_log)
        remaining = _delete_all(StorageService(), bucket, list(paths))
        if not remaining:
            if task_log is not None:
                task_log_service.mark_success(task_log, summary=f"{len(paths)} file(s) deleted")
            return
        attempt = self.request.retries + 1
        if attempt < MAX_ATTEMPTS:
            if task_log is not None:
                task_log_service.mark_retry(task_log)
            raise self.retry(kwargs={"bucket": bucket, "paths": remaining},
                             countdown=bounded_countdown(_BASE_DELAY_SECONDS * 2 ** self.request.retries))
        dead_letter_paths(db, task_id=self.request.id, bucket=bucket, paths=remaining, attempts=attempt,
                          first_at=getattr(task_log, "queued_at", None) or datetime.now(timezone.utc),
                          reason=f"still failing after {attempt} attempts")
        if task_log is not None:
            task_log_service.mark_failure(task_log, f"{len(remaining)} file(s) not deleted - dead-lettered")
        logger.error("Erasure file cleanup dead-lettered: %s file(s) | task_id=%s", len(remaining), self.request.id)
    except Retry:
        raise
    except Exception:
        rollback_quietly(db)
        logger.exception("Erasure file cleanup failed | task_id=%s", self.request.id)
        raise
    finally:
        db.close()
