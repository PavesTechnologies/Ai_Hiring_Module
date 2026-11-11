"""
Email templates on the Settings page (settings phase 1). HR admins view, preview and edit each trigger
event's template; every save is a new version, and an older version can be restored (as a new version).

Guardrails: placeholders must be ones the event's sender supplies (email_rendering.PLACEHOLDERS), plain
{name} fields only; the preview renders made-up sample values and sends nothing - there is no test send.
The audit records the event, the version and template ids only; the wording stays in the versions.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.enums.constants import ActionType, EntityType
from app.exceptions.campaign_exceptions import CampaignException
from app.models.email import EmailTemplate, EmailTriggerEvent
from app.models.identity import User, UserRole
from app.services.notifications.email_rendering import (
    NOT_SENT, PLACEHOLDERS, SAMPLE_VALUES, SUBJECT_MAX, render, template_problems,
)

_AUDIT_ENTITY_ID = UUID("00000000-0000-0000-0000-000000000000")
NAME_MAX = 255
BODY_MAX = 100_000

# Who receives each email - shown on the tab so HR knows the audience of the wording.
AUDIENCE = {
    EmailTriggerEvent.CANDIDATE_REJECTED: "Candidate", EmailTriggerEvent.CANDIDATE_SELECTED: "Candidate",
    EmailTriggerEvent.INTERVIEW_SCHEDULED: "Candidate", EmailTriggerEvent.INTERVIEW_RESCHEDULED: "Candidate",
    EmailTriggerEvent.INTERVIEW_CANCELLED: "Candidate",
    EmailTriggerEvent.INTERVIEW_FEEDBACK_REQUESTED: "Interviewer",
    EmailTriggerEvent.INTERVIEW_INTERVIEWER_INVITATION: "Interviewer",
    EmailTriggerEvent.INTERVIEW_INTERVIEWER_RESCHEDULED: "Interviewer",
    EmailTriggerEvent.INTERVIEW_INTERVIEWER_REMOVED: "Interviewer",
    EmailTriggerEvent.INTERVIEW_INTERVIEWER_CANCELLED: "Interviewer",
    EmailTriggerEvent.EMBEDDING_HEALTH_ALERT: "HR admins", EmailTriggerEvent.EMAIL_SEND_STUCK_ALERT: "HR admins",
    EmailTriggerEvent.UPLOAD_PERMANENTLY_FAILED: "Uploader and HR admins (not sent yet)",
}


def _event(trigger_event: str) -> EmailTriggerEvent:
    try:
        return EmailTriggerEvent(trigger_event)
    except ValueError:
        raise CampaignException("Unknown email.", 404)


def _label(event: EmailTriggerEvent) -> str:
    return event.value.replace("_", " ").capitalize()


def sample_values(event: EmailTriggerEvent) -> dict:
    return {name: SAMPLE_VALUES[name] for name in PLACEHOLDERS.get(event, ())}


class EmailTemplateSettingsService:
    def __init__(self, db, audit_service):
        self.db = db
        self.audit_service = audit_service

    def _versions(self, event: EmailTriggerEvent) -> list[EmailTemplate]:
        return (self.db.query(EmailTemplate).filter(EmailTemplate.trigger_event == event)
                .order_by(EmailTemplate.version.desc(), EmailTemplate.created_at.desc()).all())

    def list(self) -> list[dict]:
        rows = self.db.query(EmailTemplate).all()
        result = []
        for event in EmailTriggerEvent:
            versions = [r for r in rows if r.trigger_event == event]
            active = next((r for r in versions if r.is_active), None)
            result.append({
                "trigger_event": event.value, "label": _label(event), "audience": AUDIENCE.get(event),
                "editable": event not in NOT_SENT, "sent_by_the_system": event not in NOT_SENT,
                "active": None if active is None else {"id": str(active.id), "name": active.name,
                                                       "subject": active.subject, "version": active.version},
                "versions": len(versions),
            })
        return result

    def get(self, trigger_event: str) -> dict:
        event = _event(trigger_event)
        versions = self._versions(event)
        names = dict(self.db.query(User.id, User.full_name)
                     .filter(User.id.in_({v.created_by for v in versions if v.created_by})).all()) \
            if any(v.created_by for v in versions) else {}
        active = next((v for v in versions if v.is_active), None)
        return {
            "trigger_event": event.value, "label": _label(event), "audience": AUDIENCE.get(event),
            "editable": event not in NOT_SENT,
            "placeholders": list(PLACEHOLDERS.get(event, ())), "sample_values": sample_values(event),
            "active": None if active is None else {"id": str(active.id), "name": active.name,
                                                   "subject": active.subject, "body": active.body_template,
                                                   "version": active.version},
            "versions": [{"id": str(v.id), "version": v.version, "name": v.name, "subject": v.subject,
                          "body": v.body_template, "is_active": v.is_active,
                          "created_at": v.created_at.isoformat() if v.created_at else None,
                          "created_by": names.get(v.created_by)} for v in versions],
        }

    def _validated(self, event: EmailTriggerEvent, name, subject, body) -> tuple[str, str, str]:
        if event in NOT_SENT:
            raise CampaignException("Nothing sends this email yet, so its template can't be edited.", 422)
        problems = {}
        name = (name or "").strip() if isinstance(name, str) else ""
        subject = (subject or "").strip() if isinstance(subject, str) else ""
        body = body if isinstance(body, str) else ""
        if not 1 <= len(name) <= NAME_MAX:
            problems["name"] = [f"Give a name of 1 to {NAME_MAX} characters."]
        if not 1 <= len(subject) <= SUBJECT_MAX:
            problems["subject"] = [f"Give a subject of 1 to {SUBJECT_MAX} characters."]
        elif template_problems(subject, PLACEHOLDERS[event]):
            problems["subject"] = template_problems(subject, PLACEHOLDERS[event])
        if not body.strip() or len(body) > BODY_MAX:
            problems["body"] = [f"Give a body of up to {BODY_MAX:,} characters."]
        elif template_problems(body, PLACEHOLDERS[event]):
            problems["body"] = template_problems(body, PLACEHOLDERS[event])
        if problems:
            raise CampaignException("The template can't be used as it is.", 422, data={"problems": problems})
        return name, subject, body

    def preview(self, trigger_event: str, payload: dict) -> dict:
        """The email as it would look with made-up values. Sends nothing."""
        event = _event(trigger_event)
        name, subject, body = self._validated(event, payload.get("name") or "Preview", payload.get("subject"),
                                              payload.get("body"))
        from app.tasks.email_tasks import _html_escape_context      # the send's own escaping
        values = sample_values(event)
        return {"subject": render(subject, values), "html": render(body, _html_escape_context(values)),
                "sample_values": values}

    def _save_version(self, event: EmailTriggerEvent, name: str, subject: str, body: str, *, actor_id: str,
                      restored_from: EmailTemplate | None = None) -> dict:
        versions = self._versions(event)
        active = next((v for v in versions if v.is_active), None)
        if active is not None and (active.name, active.subject, active.body_template) == (name, subject, body):
            return {"changed": False, **self.get(event.value)}
        next_version = (self.db.query(func.max(EmailTemplate.version))
                        .filter(EmailTemplate.trigger_event == event).scalar() or 0) + 1
        now = datetime.now(timezone.utc)
        try:
            if active is not None:
                active.is_active = False
                active.updated_at = now
                self.db.flush()                        # one active row per event (partial unique index)
            new = EmailTemplate(trigger_event=event, name=name, subject=subject, body_template=body,
                                is_active=True, version=next_version, created_by=actor_id, updated_at=now)
            self.db.add(new)
            self.db.flush()
            details = {"title": "Email template restored" if restored_from else "Email template updated",
                       "trigger_event": event.value, "version": next_version,
                       "from_template_id": str(active.id) if active else None, "to_template_id": str(new.id)}
            if restored_from is not None:
                details["restored_from_version"] = restored_from.version
            self.audit_service.log(
                actor_id=actor_id, actor_role=UserRole.HR_ADMIN.value,
                action_type=ActionType.PLATFORM_CONFIG_UPDATED.value, entity_type=EntityType.PLATFORM_CONFIG.value,
                entity_id=_AUDIT_ENTITY_ID, campaign_id=None, details=details)
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            raise CampaignException("Someone else saved this template at the same time. Reload and try again.",
                                    409)
        except Exception:
            self.db.rollback()
            raise
        return {"changed": True, **self.get(event.value)}

    def save(self, trigger_event: str, payload: dict, *, actor_id: str) -> dict:
        event = _event(trigger_event)
        name, subject, body = self._validated(event, payload.get("name"), payload.get("subject"),
                                              payload.get("body"))
        return self._save_version(event, name, subject, body, actor_id=actor_id)

    def restore(self, trigger_event: str, template_id: str, *, actor_id: str) -> dict:
        event = _event(trigger_event)
        try:
            row = self.db.get(EmailTemplate, UUID(str(template_id)))
        except ValueError:
            row = None
        if row is None or row.trigger_event != event:
            raise CampaignException("Unknown version.", 404)
        if row.is_active:
            raise CampaignException("That version is already in use.", 422)
        name, subject, body = self._validated(event, row.name, row.subject, row.body_template)
        return self._save_version(event, name, subject, body, actor_id=actor_id, restored_from=row)
