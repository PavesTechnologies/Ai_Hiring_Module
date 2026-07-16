from datetime import datetime
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.async_tasks import BulkUploadJob, BulkUploadStatus


class BulkUploadJobRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, job: BulkUploadJob) -> BulkUploadJob:
        self.db.add(job)
        self.db.flush()
        self.db.refresh(job)
        return job

    def get_by_id(self, job_id: UUID) -> BulkUploadJob | None:
        return self.db.get(BulkUploadJob, job_id)

    def update_status(
        self,
        job_id: UUID,
        status: BulkUploadStatus,
        error_summary: str | None = None,
        completed_at: datetime | None = None,
    ) -> None:
        values = {"status": status}
        if error_summary is not None:
            values["error_summary"] = error_summary
        if completed_at is not None:
            values["completed_at"] = completed_at

        self.db.execute(
            update(BulkUploadJob)
            .where(BulkUploadJob.id == job_id)
            .values(**values)
        )
        self.db.flush()

    def set_total_files(self, job_id: UUID, total_files: int) -> None:
        self.db.execute(
            update(BulkUploadJob)
            .where(BulkUploadJob.id == job_id)
            .values(total_files=total_files)
        )
        self.db.flush()

    def increment_queued_count(self, job_id: UUID, by: int = 1) -> None:
        self._atomic_increment(job_id, BulkUploadJob.queued_count, by)

    def increment_processed_count(self, job_id: UUID, by: int = 1) -> None:
        self._atomic_increment(job_id, BulkUploadJob.processed_count, by)

    def increment_failed_count(self, job_id: UUID, by: int = 1) -> None:
        self._atomic_increment(job_id, BulkUploadJob.failed_count, by)

    def increment_duplicate_count(self, job_id: UUID, by: int = 1) -> None:
        self._atomic_increment(job_id, BulkUploadJob.duplicate_count, by)

    def _atomic_increment(self, job_id: UUID, column, by: int) -> None:
        """
        SQL-level UPDATE x = x + :by, not an ORM read-modify-write — the
        epic explicitly requires this to avoid lost updates when multiple
        RESUME_PARSE tasks for the same bulk job complete concurrently.
        """
        self.db.execute(
            update(BulkUploadJob)
            .where(BulkUploadJob.id == job_id)
            .values(**{column.key: column + by})
        )
        self.db.flush()

    def get_counts(self, job_id: UUID) -> tuple[int, int, int, int] | None:
        """Returns (total_files, processed_count, failed_count, duplicate_count)."""
        job = self.get_by_id(job_id)
        if job is None:
            return None
        return job.total_files, job.processed_count, job.failed_count, job.duplicate_count

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
