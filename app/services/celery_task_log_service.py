import json
import logging
from datetime import datetime, timezone

from app.models.async_tasks import (
    CeleryTaskLog,
    TaskStatus,
)
from app.repositories.celery_task_log_repository import (
    CeleryTaskLogRepository,
)
from app.repositories.dead_letter_queue_repository import DeadLetterQueueRepository

logger = logging.getLogger(__name__)


class CeleryTaskLogService:

    def __init__(self, repository: CeleryTaskLogRepository):
        self.repository = repository

    def create_log(
        self,
        task_id: str,
        task_type: str,
        created_by: str | None = None,
        title: str | None = None,
        idempotency_key: str | None = None,
        resume_id=None,
        campaign_candidate_id=None,
        jd_id=None,
    ) -> CeleryTaskLog:
        """
        Called synchronously from the route before the Celery task is even
        queued, so the upload is visible (with who submitted it and its
        title) to a "my uploads" style listing from the moment the request
        is accepted — not just once a worker picks it up. Status starts at
        QUEUED; the task itself flips it to RUNNING when it actually starts.

        idempotency_key/resume_id/campaign_candidate_id/jd_id are optional
        and default to None, so every existing call site is unaffected.
        """
        log = CeleryTaskLog(
            task_id=task_id,
            task_type=task_type,
            created_by=created_by,
            title=title,
            idempotency_key=idempotency_key,
            resume_id=resume_id,
            campaign_candidate_id=campaign_candidate_id,
            jd_id=jd_id,
            status=TaskStatus.QUEUED,
        )

        log = self.repository.create(log)
        self.repository.commit()          # <-- IMPORTANT

        return log

    def mark_running(
        self,
        log: CeleryTaskLog,
    ) -> CeleryTaskLog:
        log.status = TaskStatus.RUNNING
        log.started_at = log.started_at or datetime.now(timezone.utc)

        log = self.repository.update(log)
        self.repository.commit()          # <-- IMPORTANT

        return log

    def mark_success(
        self,
        log: CeleryTaskLog,
        summary: str,
    ) -> CeleryTaskLog:

        log.status = TaskStatus.SUCCESS
        log.output_summary = summary
        log.completed_at = datetime.now(timezone.utc)

        log = self.repository.update(log)
        self._resolve_dead_letters(log)
        self.repository.commit()          # <-- IMPORTANT

        return log

    def _resolve_dead_letters(self, log: CeleryTaskLog) -> None:
        """
        This task type now succeeded for this candidate/resume, so any
        earlier dead-lettered attempts of it are resolved - kept for audit
        and the replay-limit count, but dropped from the open DLQ view and
        no longer replayable. Covers DLQ replays and any other later
        success (e.g. a manual rescore) alike.

        Runs in a SAVEPOINT inside the success commit: adds no commit of its
        own, and a failure here is logged and rolled back to the savepoint
        without affecting the task's SUCCESS write.
        """
        campaign_candidate_id = getattr(log, "campaign_candidate_id", None)
        resume_id = getattr(log, "resume_id", None)
        if campaign_candidate_id is None and resume_id is None:
            return
        db = getattr(self.repository, "db", None)
        if db is None:
            return
        try:
            with db.begin_nested():
                DeadLetterQueueRepository(db).resolve_open(
                    task_type=log.task_type,
                    campaign_candidate_id=campaign_candidate_id,
                    resume_id=resume_id,
                    resolved_at=log.completed_at or datetime.now(timezone.utc),
                    resolution_notes=f"Resolved: re-run succeeded (task {log.task_id}).",
                )
        except Exception:
            logger.exception(
                "Failed to resolve dead-letter entries for task_id=%s task_type=%s",
                getattr(log, "task_id", None), getattr(log, "task_type", None),
            )

    def mark_failure(
        self,
        log: CeleryTaskLog,
        error: str,
    ) -> CeleryTaskLog:

        log.status = TaskStatus.FAILURE
        log.error_message = error
        log.completed_at = datetime.now(timezone.utc)

        log = self.repository.update(log)
        self.repository.commit()          # <-- IMPORTANT

        return log

    def mark_retry(
        self,
        log: CeleryTaskLog,
    ) -> CeleryTaskLog:

        log.status = TaskStatus.RETRY
        log.retry_count += 1

        log = self.repository.update(log)
        self.repository.commit()          # <-- IMPORTANT

        return log

    def mark_dispatch_failed(
        self,
        log: CeleryTaskLog,
        error_message: str,
    ) -> CeleryTaskLog:
        """
        Resume-upload resilience: apply_async() itself raised (broker
        unreachable) - status stays QUEUED (this is not a terminal
        failure, unlike mark_failure/mark_dead - a recovery job is
        expected to redispatch it later), only dispatch_failed and the
        error detail change.
        """
        log.dispatch_failed = True
        log.error_message = error_message
        log.output_summary = json.dumps({"enqueue_failed": True, "reason": error_message})

        log = self.repository.update(log)
        self.repository.commit()          # <-- IMPORTANT

        return log

    def mark_paused(
        self,
        log: CeleryTaskLog,
    ) -> CeleryTaskLog:
        """
        TaskStatus.PAUSED doubles as "soft-cancelled" (see its definition) —
        used here when a per-file bulk-upload task finds its file was
        cancelled before it got a chance to run.
        """
        log.status = TaskStatus.PAUSED
        log.completed_at = datetime.now(timezone.utc)

        log = self.repository.update(log)
        self.repository.commit()          # <-- IMPORTANT

        return log

    def mark_dead(
        self,
        log: CeleryTaskLog,
        error_message: str,
    ) -> CeleryTaskLog:
        """
        M07-E03 S01 T03: a still-QUEUED downstream task (e.g. AI_EVALUATE)
        that must never run because its candidate was already rejected at
        an earlier layer - TaskStatus.DEAD is terminal (unlike PAUSED,
        which implies a resumable soft-cancel), matching this ticket's
        "Cancel it" wording exactly.
        """
        log.status = TaskStatus.DEAD
        log.error_message = error_message
        log.completed_at = datetime.now(timezone.utc)

        log = self.repository.update(log)
        self.repository.commit()          # <-- IMPORTANT

        return log