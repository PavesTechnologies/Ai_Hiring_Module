from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.async_tasks import BulkUploadFileStatus, BulkUploadJob, BulkUploadJobFile

_SORT_COLUMNS = {
    "created_at": BulkUploadJobFile.created_at,
    "status": BulkUploadJobFile.status,
    "original_filename": BulkUploadJobFile.original_filename,
}


class BulkUploadJobFileRepository:
    def __init__(self, db: Session):
        self.db = db

    def create_many(self, files: list[BulkUploadJobFile]) -> list[BulkUploadJobFile]:
        self.db.add_all(files)
        self.db.flush()
        return files

    def get_by_id(self, file_id: UUID) -> BulkUploadJobFile | None:
        return self.db.get(BulkUploadJobFile, file_id)

    def get_by_id_and_job(self, file_id: UUID, bulk_upload_job_id: UUID) -> BulkUploadJobFile | None:
        """Validates the file actually belongs to that job, not just that the file id exists."""
        stmt = select(BulkUploadJobFile).where(
            BulkUploadJobFile.id == file_id,
            BulkUploadJobFile.bulk_upload_job_id == bulk_upload_job_id,
        )
        return self.db.execute(stmt).scalars().first()

    def get_by_job_id(self, bulk_upload_job_id: UUID) -> list[BulkUploadJobFile]:
        return (
            self.db.query(BulkUploadJobFile)
            .filter(BulkUploadJobFile.bulk_upload_job_id == bulk_upload_job_id)
            .all()
        )

    def search(
        self,
        *,
        bulk_upload_job_id: UUID,
        status: BulkUploadFileStatus | None = None,
        search: str | None = None,
        page: int = 1,
        size: int = 20,
        sort_by: str = "created_at",
        sort_dir: str = "desc",
    ) -> list[BulkUploadJobFile]:
        """Monitoring-only, no writes. Backs GET /bulk-uploads/{id}/files' list/search/filter."""
        conditions = self._build_search_conditions(bulk_upload_job_id, status, search)
        sort_column = _SORT_COLUMNS.get(sort_by, BulkUploadJobFile.created_at)
        order = sort_column.asc() if sort_dir == "asc" else sort_column.desc()

        stmt = (
            select(BulkUploadJobFile)
            .where(*conditions)
            .order_by(order)
            .offset((page - 1) * size)
            .limit(size)
        )
        return list(self.db.execute(stmt).scalars().all())

    def count_search(
        self,
        *,
        bulk_upload_job_id: UUID,
        status: BulkUploadFileStatus | None = None,
        search: str | None = None,
    ) -> int:
        conditions = self._build_search_conditions(bulk_upload_job_id, status, search)
        stmt = select(func.count()).select_from(BulkUploadJobFile).where(*conditions)
        return self.db.execute(stmt).scalar_one()

    @staticmethod
    def _build_search_conditions(
        bulk_upload_job_id: UUID,
        status: BulkUploadFileStatus | None,
        search: str | None,
    ) -> list:
        conditions = [BulkUploadJobFile.bulk_upload_job_id == bulk_upload_job_id]
        if status is not None:
            conditions.append(BulkUploadJobFile.status == status)
        if search:
            conditions.append(BulkUploadJobFile.original_filename.ilike(f"%{search}%"))
        return conditions

    def count_by_status(self, status: BulkUploadFileStatus, campaign_id: UUID | None = None) -> int:
        """
        Monitoring-only, job-agnostic (unlike count_search, which is always
        scoped to one job) — backs queue-status' bulk_files_queued/running,
        which are counted across every job, optionally scoped to one
        campaign via bulk_upload_job_files -> bulk_upload_jobs.campaign_id.
        """
        conditions = [BulkUploadJobFile.status == status]
        if campaign_id is not None:
            job_ids_in_campaign = select(BulkUploadJob.id).where(BulkUploadJob.campaign_id == campaign_id)
            conditions.append(BulkUploadJobFile.bulk_upload_job_id.in_(job_ids_in_campaign))
        stmt = select(func.count()).select_from(BulkUploadJobFile).where(*conditions)
        return self.db.execute(stmt).scalar_one()

    def update_status(self, file_id: UUID, status: BulkUploadFileStatus) -> None:
        self.db.execute(
            update(BulkUploadJobFile)
            .where(BulkUploadJobFile.id == file_id)
            .values(status=status)
        )
        self.db.flush()

    def try_start_processing(self, file_id: UUID) -> bool:
        """
        Atomically claims QUEUED (or RUNNING) -> RUNNING. RUNNING is a valid
        source state too because a transient stage failure inside
        parse_bulk_upload_file triggers a real Celery retry of the *same*
        task_id — and by the time that retry re-enters this function, its
        own first attempt already flipped the file to RUNNING. Rejecting
        that as "no longer QUEUED" (the original behavior) permanently
        stranded the file: the retry would pause instead of re-attempting,
        no further retry was ever scheduled, and the file/job sat stuck at
        RUNNING/PROCESSING forever.

        Returns False only when the file has reached a genuinely different,
        terminal state (CANCELLED/PROCESSED/FAILED) — most commonly because
        cancel_queued_files got there first while the file was still
        QUEUED. cancel_queued_files deliberately never touches an
        already-RUNNING row, so a RUNNING file reaching this method can
        only be this same task's own earlier attempt, never a genuine
        conflict with another claim.
        """
        result = self.db.execute(
            update(BulkUploadJobFile)
            .where(
                BulkUploadJobFile.id == file_id,
                BulkUploadJobFile.status.in_(
                    (BulkUploadFileStatus.QUEUED, BulkUploadFileStatus.RUNNING)
                ),
            )
            .values(status=BulkUploadFileStatus.RUNNING)
        )
        self.db.flush()
        return (result.rowcount or 0) > 0

    def cancel_queued_files(self, bulk_upload_job_id: UUID) -> int:
        """
        Bulk-cancels every still-QUEUED file for this job. Files already
        PROCESSED/FAILED are untouched, and a file whose per-file task is
        already running is left alone too — it finishes naturally, mirroring
        CampaignRepository.suspend_queued_tasks's exact "leave RUNNING work
        alone" behavior for campaign pause. Returns the number cancelled.
        """
        result = self.db.execute(
            update(BulkUploadJobFile)
            .where(
                BulkUploadJobFile.bulk_upload_job_id == bulk_upload_job_id,
                BulkUploadJobFile.status == BulkUploadFileStatus.QUEUED,
            )
            .values(status=BulkUploadFileStatus.CANCELLED)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount or 0

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
