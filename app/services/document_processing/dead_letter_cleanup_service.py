import logging

from app.exception_handler.exceptions import ConflictError, NotFoundError
from app.models.async_tasks import CeleryTaskLog, DeadLetterQueue, TaskStatus
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.checkpoint_repository import CheckpointRepository
from app.repositories.dead_letter_queue_repository import DeadLetterQueueRepository
from app.repositories.document_processing_repository import DocumentProcessingRepository
from app.repositories.stage_failure_log_repository import StageFailureLogRepository

logger = logging.getLogger(__name__)

# Mirrors JDService.JD_STORAGE_BUCKET - not imported directly to avoid pulling
# JDService's full dependency chain into this admin-only cleanup service for
# one bucket-name constant.
_JD_STORAGE_BUCKET = "airs-job-descriptions"
_JD_TASK_TYPE = "JD_DOCUMENT_PROCESSING"


class DeadLetterCleanupService:
    """
    Permanently purges a failed task's retry-tracking trail:
    document_processing_stage_executions, document_processing_checkpoints,
    and stage_failure_logs rows for that task_id, plus whichever of
    dead_letter_queue / celery_task_log actually applies. No undo.

    Two failure shapes exist in this codebase, handled differently:

    1. Dead-lettered (a dead_letter_queue row exists) - a stage failure that
       went through RetryDriver and was classified PERMANENT or exhausted
       its attempts. Deletes dead_letter_queue + tracking rows, but leaves
       celery_task_log alone so the task still shows up in upload history
       as FAILURE.

    2. Orphaned failure (celery_task_log.status == FAILURE but no
       dead_letter_queue row) - a failure that never went through
       RetryDriver at all, e.g. a DuplicateJDException, or (before this
       session's stage_execution_service.py fix) a DB error in stage
       bookkeeping outside the wrapped try/except. There is no dead-letter
       trail and no other cleanup path will ever reach these, so this also
       deletes celery_task_log itself - otherwise they'd linger forever as
       unresolvable FAILURE entries.

    Any other status (QUEUED/RUNNING/SUCCESS/RETRY/DEAD) is refused - this
    service only ever deletes tasks that have actually finished failing.

    Deliberately does NOT touch any business record (JobDescription, Resume,
    Candidate, CandidateSkill, JDUnknownSkill/UnknownSkill) - none of those
    can exist for a failed task under the current pipeline design.
    JDSkill/JDUnknownSkill rows are only ever created inside the same
    transaction as JobDescription itself (JDService.persist_processed_jd,
    called from the PERSISTENCE stage) - a task that failed never completed
    PERSISTENCE, so no jd_id (and therefore no such rows) exists to clean
    up. Same reasoning covers CandidateSkill's unknown_skill_id on the
    resume side.

    Only deletes the uploaded document from storage for dead-lettered JD
    tasks, and only when a checkpoint snapshot captured its file_path. A
    JD's JobDescription row doesn't exist until PERSISTENCE succeeds, so
    once failed, nothing references that uploaded file anymore. A resume's
    `resumes` row (and its file_path) is created up front, before its
    pipeline even starts - deleting that file here would orphan a live
    business record, so resume failures leave the file untouched. Orphaned
    failures never had a checkpoint written, so no file_path is ever on
    record for them - the file (if any) can't be identified here at all.
    """

    def __init__(
        self,
        dead_letter_queue_repo: DeadLetterQueueRepository,
        celery_task_log_repo: CeleryTaskLogRepository,
        checkpoint_repo: CheckpointRepository,
        stage_failure_log_repo: StageFailureLogRepository,
        document_processing_repo: DocumentProcessingRepository,
        storage_service,
    ):
        self.dead_letter_queue_repo = dead_letter_queue_repo
        self.celery_task_log_repo = celery_task_log_repo
        self.checkpoint_repo = checkpoint_repo
        self.stage_failure_log_repo = stage_failure_log_repo
        self.document_processing_repo = document_processing_repo
        self.storage_service = storage_service

    def purge(self, task_id: str) -> DeadLetterQueue | CeleryTaskLog:
        entry = self.dead_letter_queue_repo.get_by_task_id(task_id)
        if entry is not None:
            return self._purge_dead_lettered(task_id, entry)

        task_log = self.celery_task_log_repo.get_by_task_id(task_id)
        if task_log is None:
            raise NotFoundError(f"No task found for '{task_id}'.")
        if task_log.status != TaskStatus.FAILURE:
            raise ConflictError(
                f"Task '{task_id}' is not a failed task (status={task_log.status.value}); refusing to delete."
            )
        return self._purge_orphaned_failure(task_id, task_log)

    def _purge_dead_lettered(self, task_id: str, entry: DeadLetterQueue) -> DeadLetterQueue:
        if entry.task_type == _JD_TASK_TYPE:
            self._delete_jd_file(entry.input_payload, task_id)

        try:
            self.stage_failure_log_repo.delete_by_task_id(task_id)
            self.document_processing_repo.delete_by_task_id(task_id)
            self.checkpoint_repo.delete(task_id)
            self.dead_letter_queue_repo.delete_by_task_id(task_id)
            self.dead_letter_queue_repo.commit()
        except Exception:
            self.dead_letter_queue_repo.rollback()
            raise

        return entry

    def _purge_orphaned_failure(self, task_id: str, task_log: CeleryTaskLog) -> CeleryTaskLog:
        try:
            self.stage_failure_log_repo.delete_by_task_id(task_id)
            self.document_processing_repo.delete_by_task_id(task_id)
            self.checkpoint_repo.delete(task_id)
            self.celery_task_log_repo.delete_by_task_id(task_id)
            self.celery_task_log_repo.commit()
        except Exception:
            self.celery_task_log_repo.rollback()
            raise

        return task_log

    def _delete_jd_file(self, input_payload: dict | None, task_id: str) -> None:
        """
        Best-effort, same convention as every other storage-cleanup call site
        in this codebase (JDProcessingPipeline, CandidateErasureService) - a
        transient storage-provider error must not block the DB-side purge,
        it just leaves an orphaned object to clean up later.
        """
        file_path = (input_payload or {}).get("file_path")
        if not file_path:
            return
        try:
            self.storage_service.delete_file(bucket_name=_JD_STORAGE_BUCKET, file_path=file_path)
        except Exception:
            logger.exception(
                "Failed to delete JD document '%s' from storage while purging failed task '%s'.",
                file_path, task_id,
            )
