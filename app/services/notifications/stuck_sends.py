"""
Emails stuck in SENDING (batch 4 follow-up).

The send claims a row (QUEUED -> SENDING) before it calls SES. A row left SENDING means the outcome is
unknown - the request may have gone out - so it is never sent again automatically (review item 7). One
left there past STUCK_SENDING_AFTER needs a person: it shows in the HR_ADMIN monitoring panel
(monitoring_service.task_health) and in the stuck-send alert (app/tasks/email_health_tasks.py).

Counts and trigger types only - no recipient details.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from app.enums.constants import ActionType, EntityType
from app.models.email import EmailNotification, EmailNotificationStatus, EmailTriggerEvent
from app.repositories.email_notification_repository import EmailNotificationRepository

STUCK_SENDING_AFTER = timedelta(minutes=30)

# The alert's own emails are not counted for the alert (a stuck alert must not raise another alert);
# the monitoring panel still shows them.
ALERT_TRIGGER = EmailTriggerEvent.EMAIL_SEND_STUCK_ALERT


def _value(trigger) -> str:
    return getattr(trigger, "value", trigger)


def stuck_sends(db, *, now: datetime | None = None, for_alert: bool = False) -> dict:
    """{count, oldest_since, newest_since, by_trigger, stuck_after_minutes} for rows SENDING too long."""
    now = now or datetime.now(timezone.utc)
    rows = EmailNotificationRepository(db).get_stuck_sending(
        now - STUCK_SENDING_AFTER, exclude_triggers=(ALERT_TRIGGER,) if for_alert else ())
    return {
        "count": sum(r[1] for r in rows),
        "oldest_since": min((r[2] for r in rows), default=None),
        "newest_since": max((r[3] for r in rows), default=None),
        "by_trigger": [{"trigger_event": _value(r[0]), "count": r[1]}
                       for r in sorted(rows, key=lambda r: (-r[1], _value(r[0])))],
        "stuck_after_minutes": int(STUCK_SENDING_AFTER.total_seconds() // 60),
    }


def should_alert(stuck: dict, last_alert_at: datetime | None, now: datetime, repeat_hours: float) -> bool:
    """
    Alert when something is stuck and either:
    - no alert has gone out yet, or an email became stuck after the last alert (a new problem); or
    - the last alert is repeat_hours old and emails are still stuck (a reminder).
    """
    if not stuck["count"]:
        return False
    if last_alert_at is None:
        return True
    newest = stuck["newest_since"]
    if newest is not None and newest + STUCK_SENDING_AFTER > last_alert_at:
        return True
    return now - last_alert_at >= timedelta(hours=repeat_hours)


RESOLVABLE_OUTCOMES = ("SENT", "FAILED")


def _since(row):
    return row.sending_since or row.created_at


def list_stuck(db, *, now: datetime | None = None) -> list[dict]:
    """The emails stuck in SENDING, oldest first: ids, type, times and the SES message id - no recipient."""
    now = now or datetime.now(timezone.utc)
    since = func.coalesce(EmailNotification.sending_since, EmailNotification.created_at)
    rows = (db.query(EmailNotification)
            .filter(EmailNotification.status == EmailNotificationStatus.SENDING, since < now - STUCK_SENDING_AFTER)
            .order_by(since).all())
    return [{
        "id": str(r.id),
        "trigger_event": _value(r.trigger_event),
        "recipient_type": _value(r.recipient_type),
        "campaign_candidate_id": str(r.campaign_candidate_id) if r.campaign_candidate_id else None,
        "sending_since": _since(r).isoformat() if _since(r) else None,
        "minutes": int((now - _since(r)).total_seconds() // 60) if _since(r) else None,
        "provider_message_id": r.provider_message_id,
        "error_reason": r.error_reason,
    } for r in rows]


class StuckEmailError(Exception):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.message, self.status_code = message, status_code


def resolve_stuck(db, notification_id, *, outcome: str, note: str | None, actor_id: str, actor_role: str,
                  audit_service) -> dict:
    """
    Resolves an email left in SENDING after a person checked the email provider: SENT (delivered) or FAILED
    (not delivered - re-send it through the business action). Only a SENDING row; audited; committed; sends
    nothing. FAILED frees the email's duplicate-protection key, so a later automatic queuer may send it again.
    """
    outcome = (outcome or "").upper()
    if outcome not in RESOLVABLE_OUTCOMES:
        raise StuckEmailError("outcome must be SENT or FAILED.", 422)
    row = db.query(EmailNotification).filter(EmailNotification.id == notification_id).with_for_update().first()
    if row is None:
        raise StuckEmailError("Email notification not found.", 404)
    if row.status != EmailNotificationStatus.SENDING:
        raise StuckEmailError(f"Only an email in SENDING can be resolved; this one is {_value(row.status)}.", 409)
    if outcome == "SENT":
        row.status = EmailNotificationStatus.SENT
        row.sent_at = row.sent_at or datetime.now(timezone.utc)
    else:
        row.status = EmailNotificationStatus.FAILED
    row.error_reason = f"Resolved as {outcome} by an HR admin after checking the email provider."
    audit_service.log(
        actor_id=actor_id, actor_role=actor_role, action_type=ActionType.EMAIL_SEND_RESOLVED.value,
        entity_type=EntityType.EMAIL_NOTIFICATION.value, entity_id=row.id, campaign_id=None,
        details={"outcome": outcome, "trigger_event": _value(row.trigger_event),
                 "provider_message_id": row.provider_message_id, "note": (note or "").strip()[:500] or None},
    )
    db.commit()
    return {"id": str(row.id), "status": _value(row.status)}
