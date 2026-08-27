from datetime import datetime, timezone
from uuid import UUID

from app.models.async_tasks import FailureClassification
from app.services.document_processing.error_classifier import classify
from app.services.document_processing.retry_policy import DEFAULT_POLICY, STAGE_POLICIES, compute_backoff_seconds

# Celery's own Task.retry() treats max_retries=None as "fall back to this
# task's configured max_retries" (its class default of 3 for every @task
# here, since none of them override it) - NOT "no limit". Since
# STAGE_POLICIES can allow more attempts than that (AI_EXTRACTION: 5), a
# retry() call past Celery's own default silently re-raises the original
# error instead of rescheduling, well before this driver's own
# attempt_number >= policy.max_attempts check below ever gets to decide
# that. Passing this ceiling instead keeps Celery permissive so
# policy.max_attempts (checked below, and only below) remains the one
# place retry-vs-give-up is actually decided. Same pattern already used by
# embedding_tasks.py's own retry() calls (_CELERY_MAX_RETRIES_CEILING).
_CELERY_MAX_RETRIES_CEILING = 1000


class RetryDriver:
    def __init__(self, checkpoint_repo, stage_failure_log_repo, dead_letter_queue_repo, celery_task_log_service, task_log, task_type: str):
        self.checkpoint_repo = checkpoint_repo
        self.stage_failure_log_repo = stage_failure_log_repo
        self.dead_letter_queue_repo = dead_letter_queue_repo
        self.celery_task_log_service = celery_task_log_service
        self.task_log = task_log
        self.task_type = task_type

    def handle_failure(
        self,
        celery_task,
        task_id: str,
        document_type,
        error,
        attempt_number: int,
        resume_id: UUID | None = None,
    ) -> bool:
        classification = classify(error.original)
        self.stage_failure_log_repo.record(
            task_id,
            error.stage,
            attempt_number,
            type(error.original).__name__,
            str(error.original),
            classification,
        )
        self.stage_failure_log_repo.commit()

        policy = STAGE_POLICIES.get(error.stage, DEFAULT_POLICY)
        if classification == FailureClassification.PERMANENT or attempt_number >= policy.max_attempts:
            checkpoint = self.checkpoint_repo.get(task_id)
            # Checkpoint rows are intentionally NOT deleted on dead-letter: there is no
            # replay mechanism yet to re-queue DeadLetterQueue entries, and a future
            # replay feature would need this checkpoint's context_data intact.
            self.dead_letter_queue_repo.create(
                original_task_id=task_id,
                task_type=self.task_type,
                final_error_message=str(error.original),
                full_error_trace=None,
                input_payload=checkpoint.context_data if checkpoint else None,
                retry_count=attempt_number,
                first_attempted_at=self.task_log.queued_at,
                last_attempted_at=datetime.now(timezone.utc),
                # Epic 4 (M05-E04) Phase D10 — resumes never populate
                # checkpoint.context_data (ResumeProcessingPipeline never
                # passes context/checkpoint_repo, per its own docstring),
                # so resume_id is the only reliable way a DLQ row can be
                # traced back to its resume for replay. Optional, defaults
                # to None, so JD's own call site (which never passes this)
                # is completely unaffected.
                resume_id=resume_id,
            )
            self.dead_letter_queue_repo.commit()
            return False

        self.celery_task_log_service.mark_retry(self.task_log)
        delay = compute_backoff_seconds(policy, attempt_number)
        celery_task.retry(exc=error.original, countdown=delay, max_retries=_CELERY_MAX_RETRIES_CEILING)
        return True
