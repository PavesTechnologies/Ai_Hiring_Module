from uuid import UUID

from sqlalchemy.orm import Session

from app.models.email import EmailTemplate, EmailTriggerEvent


class EmailTemplateRepository:

    def __init__(self, db: Session):
        self.db = db

    def get_active_by_trigger_event(self, trigger_event: EmailTriggerEvent) -> EmailTemplate | None:
        return (
            self.db.query(EmailTemplate)
            .filter(
                EmailTemplate.trigger_event == trigger_event,
                EmailTemplate.is_active.is_(True),
            )
            .first()
        )

    def get_by_id(self, template_id: UUID) -> EmailTemplate | None:
        return (
            self.db.query(EmailTemplate)
            .filter(EmailTemplate.id == template_id)
            .first()
        )
