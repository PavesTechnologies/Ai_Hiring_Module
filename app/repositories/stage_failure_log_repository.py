from sqlalchemy.orm import Session

from app.models.async_tasks import FailureClassification, ProcessingStage, StageFailureLog


class StageFailureLogRepository:
    def __init__(self, db: Session):
        self.db = db

    def record(
        self,
        task_id: str,
        stage: ProcessingStage,
        attempt_number: int,
        exception_type: str,
        message: str,
        classification: FailureClassification,
    ) -> StageFailureLog:
        log = StageFailureLog(
            task_id=task_id,
            stage=stage,
            attempt_number=attempt_number,
            exception_type=exception_type,
            message=message,
            classification=classification,
        )
        self.db.add(log)
        self.db.flush()
        self.db.refresh(log)
        return log

    def commit(self) -> None:
        self.db.commit()
