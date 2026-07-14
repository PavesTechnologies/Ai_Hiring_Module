from sqlalchemy.orm import Session

from app.models.async_tasks import (
    DocumentProcessingCheckpoint,
    DocumentType,
    ProcessingStage,
)


class CheckpointRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, task_id: str) -> DocumentProcessingCheckpoint | None:
        return (
            self.db.query(DocumentProcessingCheckpoint)
            .filter(DocumentProcessingCheckpoint.task_id == task_id)
            .first()
        )

    def upsert(
        self,
        task_id: str,
        document_type: DocumentType,
        failed_at_stage: ProcessingStage | None,
        context_data: dict,
    ) -> DocumentProcessingCheckpoint:
        checkpoint = self.get(task_id)
        if checkpoint is None:
            checkpoint = DocumentProcessingCheckpoint(
                task_id=task_id,
                document_type=document_type,
                failed_at_stage=failed_at_stage,
                context_data=context_data,
            )
            self.db.add(checkpoint)
        else:
            checkpoint.document_type = document_type
            checkpoint.failed_at_stage = failed_at_stage
            checkpoint.context_data = context_data

        self.db.flush()
        self.db.refresh(checkpoint)
        return checkpoint

    def delete(self, task_id: str) -> None:
        checkpoint = self.get(task_id)
        if checkpoint is not None:
            self.db.delete(checkpoint)
            self.db.flush()

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
