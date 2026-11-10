"""
The stuck-send alert (batch 4 follow-up): every 15 minutes, emails left SENDING past
STUCK_SENDING_AFTER are reported to every active HR_ADMIN - through the notifications path like every
other email (recorded in email_notifications, governed by EMAIL_DELIVERY, opt-outs honoured).

A row stays SENDING when the send's outcome is unknown; it is never re-sent automatically, so a person
has to check it. The alert carries counts and trigger types only, never recipient details.
Repeats: on a newly stuck email at once, otherwise every EMAIL_SEND_STUCK_ALERT_REPEAT_HOURS (24).
"""
import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.single_run import single_run
from app.db.session import SessionLocal, rollback_quietly
from app.models.identity import UserRole
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.email_notification_repository import EmailNotificationRepository
from app.repositories.user_repository import UserRepository
from app.services.celery_task_log_service import CeleryTaskLogService
from app.services.notifications.internal_user_emails import queue_internal_user_emails
from app.services.notifications.stuck_sends import ALERT_TRIGGER, should_alert, stuck_sends

logger = logging.getLogger(__name__)

EMAIL_SEND_STUCK_CHECK_TASK_TYPE = "EMAIL_SEND_STUCK_CHECK"
_REPEAT_HOURS_KEY = "EMAIL_SEND_STUCK_ALERT_REPEAT_HOURS"
_DEFAULT_REPEAT_HOURS = 24.0
MONITORING_PATH = "/ai-screening/dashboard"


def _read_repeat_hours(db) -> float:
    try:
        raw = ConfigRepository(db).get_configs_by_keys([_REPEAT_HOURS_KEY]).get(_REPEAT_HOURS_KEY)
        hours = float(raw) if raw is not None else _DEFAULT_REPEAT_HOURS
        return hours if hours > 0 else _DEFAULT_REPEAT_HOURS
    except Exception:
        logger.warning("%s unreadable - using %s.", _REPEAT_HOURS_KEY, _DEFAULT_REPEAT_HOURS)
        return _DEFAULT_REPEAT_HOURS


def _template_context(stuck: dict, now: datetime) -> dict:
    oldest = stuck["oldest_since"]
    base = settings.frontend_base_url.rstrip("/") if settings.frontend_base_url else ""
    return {
        "stuck_count": stuck["count"],
        "stuck_after_minutes": stuck["stuck_after_minutes"],
        "oldest_minutes": int((now - oldest).total_seconds() // 60) if oldest else "",
        "by_trigger": ", ".join(f"{t['trigger_event']} ({t['count']})" for t in stuck["by_trigger"]),
        "monitoring_link": f"{base}{MONITORING_PATH}",
    }


@celery_app.task(name="notifications.monitor_stuck_sends")
@single_run("notifications.monitor_stuck_sends")
def monitor_stuck_sends() -> None:
    db = SessionLocal()
    task_log_service = None
    task_log = None
    try:
        task_log_service = CeleryTaskLogService(CeleryTaskLogRepository(db))
        task_log = task_log_service.create_log(task_id=str(uuid4()), task_type=EMAIL_SEND_STUCK_CHECK_TASK_TYPE)
        now = datetime.now(timezone.utc)
        stuck = stuck_sends(db, now=now, for_alert=True)
        last_alert_at = EmailNotificationRepository(db).get_latest_created_at(ALERT_TRIGGER)
        queued = 0
        if should_alert(stuck, last_alert_at, now, _read_repeat_hours(db)):
            hr_admins = UserRepository(db).get_active_by_role(UserRole.HR_ADMIN)
            if hr_admins:
                queued = queue_internal_user_emails(db, hr_admins, ALERT_TRIGGER, _template_context(stuck, now))
            else:
                logger.warning("%s email(s) stuck in SENDING and no active HR_ADMIN to alert.", stuck["count"])
        if stuck["count"]:
            logger.warning("Emails stuck in SENDING over %s min: %s (%s) | alert emails queued=%s",
                           stuck["stuck_after_minutes"], stuck["count"],
                           ", ".join(f"{t['trigger_event']}={t['count']}" for t in stuck["by_trigger"]), queued)
        task_log_service.mark_success(task_log, summary=json.dumps({"stuck": stuck["count"], "alerts_queued": queued}))
    except Exception as ex:
        rollback_quietly(db)
        if task_log_service is not None and task_log is not None:
            task_log_service.mark_failure(task_log, str(ex))
        logger.exception("Stuck-send check failed")
    finally:
        db.close()
