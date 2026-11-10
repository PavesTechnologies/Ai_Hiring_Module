"""
Emails stuck in SENDING (batch 4 follow-up).

The send claims a row (QUEUED -> SENDING) before it calls SES. A row left SENDING means the outcome is
unknown - the request may have gone out - so it is never sent again automatically (review item 7). One
left there past STUCK_SENDING_AFTER needs a person: it shows in the HR_ADMIN monitoring panel
(monitoring_service.task_health) and in the stuck-send alert (app/tasks/email_health_tasks.py).

Counts and trigger types only - no recipient details.
"""
from datetime import datetime, timedelta, timezone

from app.models.email import EmailTriggerEvent
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
