"""
Email drafts (email plan, phase 3, 2026-11-13): no email goes to a candidate or an interviewer on its own.

Interview scheduled / rescheduled / cancelled, the interviewer emails, feedback requests, rejection and
selection are prepared as DRAFT rows. A person previews one (rendered exactly as it will be sent, with the
checks' problems), then clicks Send and confirms a step that names the recipient. Send:
- refuses when the email would now render differently from the preview (preview_hash, a hash of the subject,
  body and recipient - the email itself is not stored), so what was confirmed is what is sent;
- runs the checks before every email (send_policy) with the sender;
- moves the row DRAFT -> QUEUED in one conditional update, so a double click or two tabs send one email;
- dispatches the send task (which checks again just before sending) and audits EMAIL_DRAFT_SENT.

Discard moves DRAFT -> DISCARDED (audited). A newer draft for the same round and recipient replaces an unsent
older one (DISCARDED); a cancellation or a removal is only prepared for someone who was actually told about the
round. Internal HR alerts are not drafts: they stay automatic.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import update

from app.models.email import (
    QUEUED_OR_DELIVERED, EmailNotification, EmailNotificationStatus as S, EmailRecipientType, EmailTriggerEvent as E,
)
from app.services.notifications import send_policy

logger = logging.getLogger(__name__)

CANDIDATE_ROUND_EVENTS = (E.INTERVIEW_SCHEDULED, E.INTERVIEW_RESCHEDULED, E.INTERVIEW_CANCELLED)
TOLD_ABOUT_ROUND = (E.INTERVIEW_SCHEDULED, E.INTERVIEW_RESCHEDULED)
INTERVIEWER_TOLD = (E.INTERVIEW_INTERVIEWER_INVITATION, E.INTERVIEW_INTERVIEWER_RESCHEDULED)


class DraftError(Exception):
    def __init__(self, message: str, status_code: int, data: dict | None = None):
        super().__init__(message)
        self.message, self.status_code, self.data = message, status_code, data


@dataclass(frozen=True)
class Preview:
    recipient_name: str | None
    recipient_address: str | None
    subject: str
    body: str
    preview_hash: str
    problems: list


# --- preparing -----------------------------------------------------------------------------------

def as_draft(notification: EmailNotification, *, prepared_by: str | None = None) -> EmailNotification:
    notification.status = S.DRAFT
    notification.prepared_by = prepared_by
    notification.prepared_at = datetime.now(timezone.utc)
    return notification


def told_about_round(db, schedule_id, *, interviewer_id=None) -> bool:
    """Whether the candidate (or this interviewer) was actually emailed about the round (queued or sent)."""
    query = db.query(EmailNotification.id).filter(EmailNotification.interview_schedule_id == schedule_id,
                                                  EmailNotification.status.in_(QUEUED_OR_DELIVERED))
    if interviewer_id is None:
        query = query.filter(EmailNotification.recipient_type == EmailRecipientType.CANDIDATE,
                             EmailNotification.trigger_event.in_(TOLD_ABOUT_ROUND))
    else:
        query = query.filter(EmailNotification.interview_interviewer_id == interviewer_id,
                             EmailNotification.trigger_event.in_(INTERVIEWER_TOLD))
    return query.first() is not None


def discard_round_drafts(db, schedule_id, *, interviewer_id=None, candidate=False, triggers=None) -> int:
    """Unsent drafts about a round replaced by a newer state of it: DISCARDED. Returns how many."""
    query = db.query(EmailNotification).filter(EmailNotification.interview_schedule_id == schedule_id,
                                               EmailNotification.status == S.DRAFT)
    if candidate:
        query = query.filter(EmailNotification.recipient_type == EmailRecipientType.CANDIDATE)
    if interviewer_id is not None:
        query = query.filter(EmailNotification.interview_interviewer_id == interviewer_id)
    if triggers:
        query = query.filter(EmailNotification.trigger_event.in_(tuple(triggers)))
    rows = query.all()
    for row in rows:
        row.status = S.DISCARDED
        row.error_reason = "Replaced by a newer state of the round before it was sent."
    if rows:
        db.commit()
    return len(rows)


# --- previewing ----------------------------------------------------------------------------------

def preview(db, notification, *, actor=None) -> Preview:
    """The email exactly as the send task renders it, with the checks' problems for this sender."""
    from app.services.notifications.email_rendering import TemplateRenderError, render
    from app.tasks.email_tasks import _html_escape_context
    _use_the_active_template(db, notification)
    ctx = send_policy._Context.load(db, notification)
    problems = send_policy.problems(db, notification, actor=actor)
    template = db.get(send_policy.EmailTemplate, notification.template_id) if notification.template_id else None
    address = ctx.recipient_address()
    name = notification.recipient_name
    if send_policy.recipient_type(notification) == EmailRecipientType.CANDIDATE and ctx.candidate is not None:
        name = ctx._decrypt(ctx.candidate.full_name_encrypted, ctx.candidate.encryption_key_id)
    subject = body = ""
    if template is not None:
        context = ctx.render_context()
        try:
            subject = render(template.subject, context)
            body = render(template.body_template, _html_escape_context(context))
        except TemplateRenderError:
            subject = body = ""
    digest = hashlib.sha256("\x1f".join([address or "", subject, body]).encode("utf-8")).hexdigest()
    return Preview(name, address, subject, body, digest, problems)


def _use_the_active_template(db, notification) -> None:
    """A draft isn't sent yet: if its template was replaced since it was prepared, it uses the active one."""
    if notification.status != S.DRAFT or notification.id is None:
        return
    from app.repositories.email_template_repository import EmailTemplateRepository
    template = db.get(send_policy.EmailTemplate, notification.template_id) if notification.template_id else None
    if template is not None and template.is_active:
        return
    active = EmailTemplateRepository(db).get_active_by_trigger_event(notification.trigger_event)
    if active is not None and active.id != notification.template_id:
        notification.template_id = active.id
        db.commit()


# --- sending and discarding ----------------------------------------------------------------------

def _draft(db, draft_id) -> EmailNotification:
    row = db.get(EmailNotification, UUID(str(draft_id)))
    if row is None:
        raise DraftError("Email not found.", 404)
    return row


def _check_access(db, row, actor) -> None:
    """The sender must be allowed to see this candidate's campaign (and, to send, have a role for this email)."""
    from app.models.campaigns import HiringCampaign
    from app.models.pipeline import CampaignCandidate
    from app.services.campaign_access import can_access_campaign
    cc = db.get(CampaignCandidate, row.campaign_candidate_id) if row.campaign_candidate_id else None
    campaign = db.get(HiringCampaign, cc.campaign_id) if cc is not None else None
    if campaign is None or not can_access_campaign(actor, campaign):
        raise DraftError("You don't have access to this candidate's campaign.", 403)


def get_preview(db, draft_id, *, actor) -> tuple[EmailNotification, Preview]:
    row = _draft(db, draft_id)
    _check_access(db, row, actor)
    return row, preview(db, row, actor=actor)


def send(db, draft_id, *, actor, preview_hash: str, audit_service) -> EmailNotification:
    row = _draft(db, draft_id)
    _check_access(db, row, actor)
    if row.status != S.DRAFT:
        raise DraftError(f"This email is {row.status.value.lower()}, not waiting to be sent.", 409,
                         {"code": "NOT_A_DRAFT", "status": row.status.value})
    current = preview(db, row, actor=actor)
    if not preview_hash or preview_hash != current.preview_hash:
        raise DraftError("The email changed since you previewed it. Preview it again before sending.", 409,
                         {"code": "PREVIEW_CHANGED"})
    if current.problems:
        refused = send_policy.EmailRefused(current.problems)
        raise DraftError(" ".join(p.message for p in current.problems), 422, refused.as_data())
    now = datetime.now(timezone.utc)
    moved = db.execute(
        update(EmailNotification)
        .where(EmailNotification.id == row.id, EmailNotification.status == S.DRAFT)
        .values(status=S.QUEUED, confirmed_by=getattr(actor, "user_id", None), confirmed_at=now,
                preview_hash=current.preview_hash)
        .execution_options(synchronize_session=False)
    ).rowcount
    if moved != 1:
        db.rollback()
        raise DraftError("This email was already sent or discarded.", 409, {"code": "NOT_A_DRAFT"})
    _audit(db, audit_service, actor, row, "EMAIL_DRAFT_SENT")
    db.commit()
    db.refresh(row)
    from app.tasks.email_tasks import send_candidate_email_task
    send_candidate_email_task.apply_async(kwargs={"email_notification_id": str(row.id)})
    return row


def queue_confirmed(db, notification, *, actor, audit_service) -> EmailNotification | None:
    """
    For bulk rejection only, after its confirm step listed every recipient: the email is queued at once,
    recorded as confirmed by the sender. None when it's a duplicate (another send won).
    """
    from app.repositories.email_notification_repository import EmailNotificationRepository
    notification.status = S.QUEUED
    notification.confirmed_by = getattr(actor, "user_id", None)
    notification.confirmed_at = datetime.now(timezone.utc)
    row = EmailNotificationRepository(db).create_unless_duplicate(notification)
    if row is None:
        return None
    _audit(db, audit_service, actor, row, "EMAIL_DRAFT_SENT")
    db.commit()
    from app.tasks.email_tasks import send_candidate_email_task
    send_candidate_email_task.apply_async(kwargs={"email_notification_id": str(row.id)})
    return row


def discard(db, draft_id, *, actor, audit_service) -> EmailNotification:
    row = _draft(db, draft_id)
    _check_access(db, row, actor)
    moved = db.execute(
        update(EmailNotification)
        .where(EmailNotification.id == row.id, EmailNotification.status == S.DRAFT)
        .values(status=S.DISCARDED, error_reason="Discarded by a person.")
        .execution_options(synchronize_session=False)
    ).rowcount
    if moved != 1:
        db.rollback()
        raise DraftError("Only an email waiting to be sent can be discarded.", 409, {"code": "NOT_A_DRAFT"})
    _audit(db, audit_service, actor, row, "EMAIL_DRAFT_DISCARDED")
    db.commit()
    db.refresh(row)
    return row


def _audit(db, audit_service, actor, row, action: str) -> None:
    from app.enums.constants import ActionType, EntityType
    from app.models.pipeline import CampaignCandidate
    roles = list(getattr(actor, "roles", ()) or ())
    role = next((r for r in ("HR_ADMIN", "HIRING_MANAGER", "RECRUITER") if r in roles), roles[0] if roles else None)
    cc = db.get(CampaignCandidate, row.campaign_candidate_id) if row.campaign_candidate_id else None
    campaign_id = getattr(cc, "campaign_id", None)
    audit_service.log(
        actor_id=getattr(actor, "user_id", None), actor_role=role,
        action_type=getattr(ActionType, action).value, entity_type=EntityType.EMAIL_NOTIFICATION.value,
        entity_id=row.id, campaign_id=campaign_id,
        details={"trigger_event": row.trigger_event.value, "recipient_type": row.recipient_type.value,
                 "campaign_candidate_id": str(row.campaign_candidate_id) if row.campaign_candidate_id else None,
                 "interview_schedule_id": str(row.interview_schedule_id) if row.interview_schedule_id else None})


# --- listing -------------------------------------------------------------------------------------

def list_for_candidate(db, campaign_candidate_id, *, include_all: bool = False) -> list[EmailNotification]:
    """A candidate's emails waiting to be sent (and, with include_all, the rest of its email history)."""
    query = db.query(EmailNotification).filter(EmailNotification.campaign_candidate_id == campaign_candidate_id)
    if not include_all:
        query = query.filter(EmailNotification.status == S.DRAFT)
    return query.order_by(EmailNotification.created_at.desc()).all()
