from uuid import UUID

from sqlalchemy.orm import Session

from app.models.email import EmailNotification


class EmailNotificationRepository:

    def __init__(self, db: Session):
        self.db = db

    def create(self, notification: EmailNotification) -> EmailNotification:
        self.db.add(notification)
        self.db.flush()
        self.db.refresh(notification)
        return notification

    def get_by_id(self, notification_id: UUID) -> EmailNotification | None:
        return (
            self.db.query(EmailNotification)
            .filter(EmailNotification.id == notification_id)
            .first()
        )

    def update(self, notification: EmailNotification) -> EmailNotification:
        self.db.flush()
        self.db.refresh(notification)
        return notification

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
