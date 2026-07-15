from sqlalchemy.orm import Session

from app.models.async_tasks import CeleryTaskLog, TaskStatus


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

    def get_recent_by_created_by(self, created_by: str, limit: int = 50) -> list[CeleryTaskLog]:
        """
        Excludes SUCCESS: this backs the "my uploads" list, which only
        needs to surface uploads still in flight or that need attention —
        a fully successful upload already shows up as a real JD in the
        normal JD list, so repeating it here would be noise.
        """
        return (
            self.db.query(CeleryTaskLog)
            .filter(
                CeleryTaskLog.created_by == created_by,
                CeleryTaskLog.status != TaskStatus.SUCCESS,
            )
            .order_by(CeleryTaskLog.queued_at.desc())
            .limit(limit)
            .all()
        )

    def commit(self):
        self.db.commit()

    def rollback(self):
        self.db.rollback()