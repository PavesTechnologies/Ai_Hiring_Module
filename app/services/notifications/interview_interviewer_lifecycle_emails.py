import logging
from zoneinfo import ZoneInfo

from app.models.email import EmailNotification, EmailNotificationStatus, EmailRecipientType, EmailTriggerEvent
from app.repositories.email_notification_repository import EmailNotificationRepository
from app.repositories.email_template_repository import EmailTemplateRepository
from app.tasks.email_tasks import send_candidate_email_task

logger = logging.getLogger(__name__)

"""
Interviewer lifecycle follow-up - queue-and-dispatch for the 3 new
EXTERNAL_INTERVIEWER-recipient trigger events this reopens: an invitation
(sent per-interviewer when they're added to a round), a removal notice
(sent when replace_interviewers() sets is_active=false), and a
cancellation notice distinct from the candidate-facing INTERVIEW_CANCELLED.

Same shape as interview_feedback_request_emails.py's
queue_interview_feedback_requested_email - plain functions, everything
(attribute reads, dedup query, template lookup, create+commit, Celery
dispatch) inside one try/except, deduped per
(interview_schedule_id, interviewer_id, trigger_event) via the same
repository method that trigger event's own dedup uses. That per-interviewer
scoping matters here specifically because a caller (schedule()/
reschedule()) passes the SAME currently-active interviewer list on every
call - re-sending an invitation to someone already invited for this round
would be wrong, and the dedup check is what makes that safe to do rather
than requiring the caller to separately track "who's genuinely new this
call."
"""


def _notes_block(schedule) -> str:
    return f"\n\nNotes: {schedule.notes}" if schedule.notes else ""


def _reason_block(reason: str | None) -> str:
    return f"\n\nReason: {reason}" if reason else ""


def _round_context(schedule, interviewer) -> dict:
    """
    Timezone-discrepancy fix: start_at is a real UTC instant now - convert
    back to a declared zone before formatting, with the zone abbreviation
    appended (e.g. "2:00 PM IST"), same fix as
    candidate_notification_emails.py's _interview_email_context.

    Per-recipient timezone fix: localizes to THIS interviewer's own zone
    (interviewer.timezone) when set, rather than always the round's
    schedule.timezone (the scheduler's zone) - an interviewer in a
    different zone than whoever scheduled the round otherwise sees the
    wrong local time. Falls back to schedule.timezone when the
    interviewer has none set, so behavior is unchanged until one is
    captured for them.
    """
    zone_name = interviewer.timezone or schedule.timezone
    local_start = schedule.start_at.astimezone(ZoneInfo(zone_name)) if schedule.start_at else None
    return {
        "interview_date": local_start.strftime("%B %d, %Y") if local_start else "TBD",
        "interview_time": local_start.strftime("%I:%M %p %Z").lstrip("0") if local_start else "TBD",
        "interview_mode": schedule.platform.value.title() if schedule.platform else "Not specified",
    }


def _queue_interviewer_email(db, campaign_candidate, schedule, interviewer, trigger_event, extra_context) -> bool:
    try:
        notification_repo = EmailNotificationRepository(db)
        existing = notification_repo.get_by_interview_schedule_id_and_interviewer_id_and_trigger_event(
            schedule.id, interviewer.id, trigger_event,
        )
        if any(n.status in (EmailNotificationStatus.QUEUED, EmailNotificationStatus.SENT) for n in existing):
            logger.info(
                "%s email already queued/sent - skipping duplicate | interview_schedule_id=%s interviewer_id=%s",
                trigger_event.value, schedule.id, interviewer.id,
            )
            return False

        template_repo = EmailTemplateRepository(db)
        template = template_repo.get_active_by_trigger_event(trigger_event)
        if template is None:
            logger.error(
                "No active %s email template configured - skipping | interview_schedule_id=%s interviewer_id=%s",
                trigger_event.value, schedule.id, interviewer.id,
            )
            return False

        notification = notification_repo.create(EmailNotification(
            candidate_id=None,
            campaign_candidate_id=campaign_candidate.id,
            interview_schedule_id=schedule.id,
            interview_interviewer_id=interviewer.id,
            recipient_type=EmailRecipientType.EXTERNAL_INTERVIEWER,
            recipient_email=interviewer.email,
            recipient_name=interviewer.name,
            trigger_event=trigger_event,
            template_id=template.id,
            status=EmailNotificationStatus.QUEUED,
            template_context={"recipient_name": interviewer.name, **extra_context},
        ))
        notification_repo.commit()

        send_candidate_email_task.apply_async(kwargs={"email_notification_id": str(notification.id)})
        return True
    except Exception:
        logger.exception(
            "Failed to queue %s email | interview_schedule_id=%s interviewer_id=%s",
            trigger_event.value, schedule.id if schedule is not None else None,
            interviewer.id if interviewer is not None else None,
        )
        return False


def queue_interview_interviewer_invitation_email(db, campaign_candidate, schedule, interviewer) -> bool:
    return _queue_interviewer_email(
        db, campaign_candidate, schedule, interviewer, EmailTriggerEvent.INTERVIEW_INTERVIEWER_INVITATION,
        {**_round_context(schedule, interviewer), "notes_block": _notes_block(schedule)},
    )


def queue_interview_interviewer_rescheduled_email(db, campaign_candidate, schedule, interviewer) -> bool:
    """
    Reschedule-notification gap fix - sent to an interviewer who was
    already invited to this round and stays on it through a reschedule.
    INVITATION won't fire again for them (deduped per (interview_
    schedule_id, interviewer_id)), so without this they'd never learn the
    time changed. The caller (InterviewScheduleService.reschedule(), via
    _queue_interviewer_lifecycle_emails) is responsible for only calling
    this for interviewers whose invitation call returned False (already
    invited) - a genuinely new interviewer gets the new time via their
    first INVITATION instead, not both.
    """
    return _queue_interviewer_email(
        db, campaign_candidate, schedule, interviewer, EmailTriggerEvent.INTERVIEW_INTERVIEWER_RESCHEDULED,
        _round_context(schedule, interviewer),
    )


def queue_interview_interviewer_removed_email(db, campaign_candidate, schedule, interviewer) -> bool:
    return _queue_interviewer_email(
        db, campaign_candidate, schedule, interviewer, EmailTriggerEvent.INTERVIEW_INTERVIEWER_REMOVED, {},
    )


def queue_interview_interviewer_cancelled_email(db, campaign_candidate, schedule, interviewer, reason: str | None = None) -> bool:
    return _queue_interviewer_email(
        db, campaign_candidate, schedule, interviewer, EmailTriggerEvent.INTERVIEW_INTERVIEWER_CANCELLED,
        {**_round_context(schedule, interviewer), "reason_block": _reason_block(reason)},
    )
