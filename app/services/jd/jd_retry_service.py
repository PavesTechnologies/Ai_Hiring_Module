import logging
from datetime import datetime, timezone

from app.exception_handler.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.models.async_tasks import DocumentType, TaskStatus
from app.services.jd import context_serializer
from app.websocket.publisher import publish_task_reset

logger = logging.getLogger(__name__)

JD_TASK_TYPE = "JD_DOCUMENT_PROCESSING"

# Only a task that has genuinely finished failing may be replayed.
# QUEUED/RUNNING would race the worker still holding it; RETRY is already
# going to run again on its own (replaying would double-dispatch the same
# task_id); SUCCESS has a JobDescription row that this service is not
# allowed to touch. Mirrors DeadLetterCleanupService's own refusal rule.
_REPLAYABLE_STATUSES = {TaskStatus.FAILURE, TaskStatus.DEAD}


class JDRetryService:
    """
    Replays a failed JD upload from the very first stage, on the same
    task_id, after deleting the previous run's tracking rows.

    This is a *restart*, not a Celery retry. RetryDriver's own retries
    resume from the checkpoint (skipping stages already done), which is
    the right behaviour for a transient blip mid-run. Once a task has
    dead-lettered, though, what a user wants from a "retry" button is a
    clean run: no stale stage rows, no half-populated checkpoint context,
    attempt numbering back at 1.

    The original dispatch arguments are not stored on celery_task_log
    (input_payload_hash is a hash, not a payload), so the replay is
    reconstructed from the checkpoint's context_data, which captures every
    submitted field — title, jurisdiction, experience bounds, notice
    period, education criteria, file_path, prompt_template_id and the
    version/lineage fields for reprocess runs. That row is deliberately
    left behind by RetryDriver on dead-letter for exactly this purpose. A
    failure with no checkpoint (an orphaned failure — e.g. a duplicate-JD
    rejection that never reached RetryDriver) therefore cannot be replayed
    and is refused rather than guessed at.

    The uploaded file in storage is never deleted here — it is the input
    the replay needs. That is the one deliberate difference from
    DeadLetterCleanupService.purge, which deletes the file precisely
    because it is ending the task's life rather than restarting it.
    """

    def __init__(
        self,
        celery_task_log_repo,
        checkpoint_repo,
        stage_failure_log_repo,
        document_processing_repo,
        dead_letter_queue_repo,
    ):
        self.celery_task_log_repo = celery_task_log_repo
        self.checkpoint_repo = checkpoint_repo
        self.stage_failure_log_repo = stage_failure_log_repo
        self.document_processing_repo = document_processing_repo
        self.dead_letter_queue_repo = dead_letter_queue_repo

    def retry_from_start(self, task_id: str, requested_by: str):
        task_log = self._load_replayable(task_id, requested_by)

        checkpoint = self.checkpoint_repo.get(task_id)
        if checkpoint is None or not checkpoint.context_data:
            raise ConflictError(
                f"Task '{task_id}' cannot be replayed: no checkpoint of its original "
                "submission exists, so its inputs are unknown. Re-upload the document instead."
            )

        # Rebuilt (and validated) BEFORE anything is deleted — if the
        # stored context can't produce a dispatchable payload, the task
        # must be left exactly as it was rather than wiped and un-runnable.
        kwargs = self._build_dispatch_kwargs(task_id, checkpoint.context_data)

        self._purge_previous_run(task_id, requested_by)
        self._reset_task_log(task_log)

        # Dispatched only after the reset is committed: the worker can pick
        # this up immediately, and it must not observe the old FAILURE row.
        from app.tasks.jd_processing_tasks import process_jd_document

        process_jd_document.apply_async(kwargs=kwargs, task_id=task_id)

        try:
            publish_task_reset(task_id, DocumentType.JD, created_by=task_log.created_by)
        except Exception:
            # Same rule as every other publish call site: a Redis problem
            # must not fail a request whose DB work already committed.
            logger.exception("Failed to publish task.reset for task_id=%s", task_id)

        logger.info(
            "JD upload replayed from first stage | task_id=%s requested_by=%s", task_id, requested_by,
        )
        return task_log

    def _load_replayable(self, task_id: str, requested_by: str):
        task_log = self.celery_task_log_repo.get_by_task_id(task_id)
        if task_log is None:
            raise NotFoundError(f"No processing task found for task_id {task_id}.")

        if task_log.task_type != JD_TASK_TYPE:
            raise ConflictError(
                f"Task '{task_id}' is a {task_log.task_type} task; this endpoint only replays JD uploads."
            )

        # Ownership, not just role: /my-uploads is scoped to the caller's
        # own uploads, so the retry button beside a row must not be usable
        # against someone else's task_id guessed or copied from elsewhere.
        if task_log.created_by != requested_by:
            raise ForbiddenError("You can only retry your own uploads.")

        if task_log.status not in _REPLAYABLE_STATUSES:
            raise ConflictError(
                f"Task '{task_id}' has not failed (status={task_log.status.value}); "
                "only a failed upload can be retried."
            )
        return task_log

    @staticmethod
    def _build_dispatch_kwargs(task_id: str, context_data: dict) -> dict:
        """
        Reconstructs process_jd_document's kwargs from the stored context.

        Round-trips through context_serializer.from_dict rather than
        reading the dict keys directly, so this stays correct if the
        context gains or renames a field — and so a context that can no
        longer be deserialized fails here, before any deletion.

        Only the *submitted* fields are carried over. Everything a stage
        derived (text, cleaned_text, extraction, skill_matches, embedding,
        jd_id) is intentionally dropped: this is a run from the first
        stage, and re-seeding derived state is what the checkpoint-resume
        path does instead.
        """
        try:
            context = context_serializer.from_dict(context_data)
        except Exception as exc:
            raise ConflictError(
                f"Task '{task_id}' cannot be replayed: its stored submission could not be "
                f"read back ({type(exc).__name__}). Re-upload the document instead."
            ) from exc

        return {
            "task_id": task_id,
            "raw_text": context.raw_text,
            "file_path": context.file_path,
            "original_filename": context.original_filename,
            "title": context.title,
            "jurisdiction": context.jurisdiction,
            "min_experience_years": context.min_experience_years,
            "max_experience_years": context.max_experience_years,
            "notice_period": context.notice_period,
            "education_criteria": context.education_criteria,
            "created_by": context.created_by,
            "prompt_template_id": str(context.prompt_template_id) if context.prompt_template_id else None,
            "existing_jd_id": str(context.existing_jd_id) if context.existing_jd_id else None,
            "version_number": context.version_number,
            "parent_jd_id": str(context.parent_jd_id) if context.parent_jd_id else None,
            "lineage_root_id": str(context.lineage_root_id) if context.lineage_root_id else None,
        }

    def _purge_previous_run(self, task_id: str, requested_by: str) -> None:
        """
        Clears the failed run's trail: stage executions, checkpoint and
        failure logs are deleted, since the replay re-creates all three
        from attempt 1 and stale rows would otherwise be indistinguishable
        from the new run's.

        A dead_letter_queue row is stamped replayed_at/replayed_by instead
        of being deleted — the columns exist for exactly this, and the DLQ
        is the operational record of "this failed and here is what
        happened to it", which a replay should extend rather than erase.
        Its input_payload also stays available as the last snapshot of a
        run that failed.

        celery_task_log is kept and reset (below) rather than deleted, so
        the upload holds its place and task_id in the user's history — and
        so the dead_letter_queue FK to it stays valid.
        """
        try:
            self.stage_failure_log_repo.delete_by_task_id(task_id)
            self.document_processing_repo.delete_by_task_id(task_id)
            self.checkpoint_repo.delete(task_id)

            dlq_entry = self.dead_letter_queue_repo.get_by_task_id(task_id)
            if dlq_entry is not None:
                self.dead_letter_queue_repo.mark_replayed(
                    dlq_entry.id, requested_by, datetime.now(timezone.utc),
                )
            self.dead_letter_queue_repo.commit()
        except Exception:
            self.dead_letter_queue_repo.rollback()
            raise

    def _reset_task_log(self, task_log) -> None:
        """
        Returns the existing row to a freshly-queued state so /my-uploads
        renders the replay as a new run rather than a failed one that
        mysteriously started moving again. Every field the previous run
        wrote is cleared — leaving retry_count at its old value would make
        retries_remaining read 0 for a run that has its full budget.
        """
        try:
            task_log.status = TaskStatus.QUEUED
            task_log.retry_count = 0
            task_log.error_message = None
            task_log.started_at = None
            task_log.completed_at = None
            task_log.duration_ms = None
            task_log.worker_hostname = None
            task_log.output_summary = None
            task_log.dispatch_failed = False
            self.celery_task_log_repo.update(task_log)
            self.celery_task_log_repo.commit()
        except Exception:
            self.celery_task_log_repo.rollback()
            raise
