from datetime import datetime, timezone

from app.models.async_tasks import (
    CeleryTaskLog,
    TaskStatus,
)
from app.repositories.celery_task_log_repository import (
    CeleryTaskLogRepository,
)


class CeleryTaskLogService:

    def __init__(self, repository: CeleryTaskLogRepository):
        self.repository = repository

    def create_log(
        self,
        task_id: str,
        task_type: str,
        created_by: str | None = None,
        title: str | None = None,
    ) -> CeleryTaskLog:
        """
        Called synchronously from the route before the Celery task is even
        queued, so the upload is visible (with who submitted it and its
        title) to a "my uploads" style listing from the moment the request
        is accepted — not just once a worker picks it up. Status starts at
        QUEUED; the task itself flips it to RUNNING when it actually starts.
        """
        log = CeleryTaskLog(
            task_id=task_id,
            task_type=task_type,
            created_by=created_by,
            title=title,
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
        self.repository.commit()          # <-- IMPORTANT

        return log

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