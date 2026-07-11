from sqlalchemy.orm import Session

from app.models.async_tasks import CeleryTaskLog


class CeleryTaskLogRepository:

    def __init__(self, db: Session):
        self.db = db

    def create(self, log: CeleryTaskLog):

        self.db.add(log)
        self.db.flush()
        self.db.refresh(log)

        return log

    def update(self, log: CeleryTaskLog):

        self.db.flush()
        self.db.refresh(log)

        return log

    def get_by_task_id(self, task_id: str) -> CeleryTaskLog | None:
        return (
            self.db.query(CeleryTaskLog)
            .filter(CeleryTaskLog.task_id == task_id)
            .first()
        )

    def commit(self):
        self.db.commit()

    def rollback(self):
        self.db.rollback()