from uuid import UUID

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.async_tasks import BulkUploadFileStatus, BulkUploadJobFile


class BulkUploadJobFileRepository:
    def __init__(self, db: Session):
        self.db = db

    def create_many(self, files: list[BulkUploadJobFile]) -> list[BulkUploadJobFile]:
        self.db.add_all(files)
        self.db.flush()
        return files

    def get_by_id(self, file_id: UUID) -> BulkUploadJobFile | None:
        return self.db.get(BulkUploadJobFile, file_id)

    def get_by_job_id(self, bulk_upload_job_id: UUID) -> list[BulkUploadJobFile]:
        return (
            self.db.query(BulkUploadJobFile)
            .filter(BulkUploadJobFile.bulk_upload_job_id == bulk_upload_job_id)
            .all()
        )

    def update_status(self, file_id: UUID, status: BulkUploadFileStatus) -> None:
        self.db.execute(
            update(BulkUploadJobFile)
            .where(BulkUploadJobFile.id == file_id)
            .values(status=status)
        )
        self.db.flush()

    def try_start_processing(self, file_id: UUID) -> bool:
        """
        Atomically claims QUEUED -> RUNNING. Returns False if the file was
        no longer QUEUED — most commonly because cancel_queued_files got
        there first — in which case the caller must not do any real work
        (avoids the race where a bulk-cancelled file gets its status
        silently overwritten back to PROCESSED/FAILED by a task that
        started just before the cancel and would otherwise not notice).
        """
        result = self.db.execute(
            update(BulkUploadJobFile)
            .where(
                BulkUploadJobFile.id == file_id,
                BulkUploadJobFile.status == BulkUploadFileStatus.QUEUED,
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
