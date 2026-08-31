from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.async_tasks import BulkUploadJob, BulkUploadStatus
from app.models.campaigns import HiringCampaign


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

    def decrement_failed_count(self, job_id: UUID, by: int = 1) -> None:
        self._atomic_increment(job_id, BulkUploadJob.failed_count, -by)

    def requeue_after_replay(self, job_id: UUID) -> None:
        """
        Reopens a job that had already reached a terminal state
        (FAILED/PARTIAL_FAILURE) back to PROCESSING and clears completed_at,
        since a replayed file means the job is no longer fully resolved.
        _maybe_finalize_job will re-close it once every file resolves again.
        """
        self.db.execute(
            update(BulkUploadJob)
            .where(BulkUploadJob.id == job_id)
            .values(status=BulkUploadStatus.PROCESSING, completed_at=None)
        )
        self.db.flush()

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

    def list_by_campaign(self, campaign_id: UUID, offset: int, limit: int) -> list[BulkUploadJob]:
        stmt = (
            select(BulkUploadJob)
            .where(BulkUploadJob.campaign_id == campaign_id)
            .order_by(BulkUploadJob.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return self.db.execute(stmt).scalars().all()

    def count_by_campaign(self, campaign_id: UUID) -> int:
        stmt = select(func.count()).select_from(BulkUploadJob).where(
            BulkUploadJob.campaign_id == campaign_id
        )
        return self.db.execute(stmt).scalar_one()

    def count_by_status(self, status: BulkUploadStatus, campaign_id: UUID | None = None) -> int:
        """
        Epic 4 (M05-E04) Phase D12 - platform-wide when campaign_id is
        omitted (the default), matching the same optional-scoping
        convention already used by CeleryTaskLogRepository.
        count_by_task_type_and_statuses and ResumeRepository.count_search.
        """
        conditions = [BulkUploadJob.status == status]
        if campaign_id is not None:
            conditions.append(BulkUploadJob.campaign_id == campaign_id)
        stmt = select(func.count()).select_from(BulkUploadJob).where(*conditions)
        return self.db.execute(stmt).scalar_one()

    def list_by_recruiter(self, recruiter_id: str, offset: int, limit: int) -> list[tuple[BulkUploadJob, str]]:
        """
        Cross-campaign bulk-upload history for one recruiter — every job
        in every campaign where hiring_campaigns.recruiter_id matches,
        regardless of who actually uploaded_by each individual job.
        Returns (job, campaign_name) pairs so a cross-campaign listing can
        show which campaign each job belongs to without a second lookup.
        """
        stmt = (
            select(BulkUploadJob, HiringCampaign.name)
            .join(HiringCampaign, HiringCampaign.id == BulkUploadJob.campaign_id)
            .where(HiringCampaign.recruiter_id == recruiter_id)
            .order_by(BulkUploadJob.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(self.db.execute(stmt).all())

    def count_by_recruiter(self, recruiter_id: str) -> int:
        stmt = (
            select(func.count())
            .select_from(BulkUploadJob)
            .join(HiringCampaign, HiringCampaign.id == BulkUploadJob.campaign_id)
            .where(HiringCampaign.recruiter_id == recruiter_id)
        )
        return self.db.execute(stmt).scalar_one()

    def get_all_by_campaign(self, campaign_id: UUID) -> list[BulkUploadJob]:
        """Unpaginated — for history export, not for the paginated list endpoint."""
        stmt = (
            select(BulkUploadJob)
            .where(BulkUploadJob.campaign_id == campaign_id)
            .order_by(BulkUploadJob.created_at.desc())
        )
        return self.db.execute(stmt).scalars().all()

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
