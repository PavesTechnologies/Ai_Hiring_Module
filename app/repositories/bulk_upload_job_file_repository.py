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

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
