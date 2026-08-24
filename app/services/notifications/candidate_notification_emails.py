import logging
from zoneinfo import ZoneInfo

from app.models.email import EmailNotification, EmailNotificationStatus, EmailTriggerEvent
from app.repositories.email_notification_repository import EmailNotificationRepository
from app.repositories.email_template_repository import EmailTemplateRepository
from app.tasks.email_tasks import send_candidate_email_task

logger = logging.getLogger(__name__)

"""
Epic 5 Step 2 - queue-and-dispatch for the 4 real candidate-facing
trigger events (INTERVIEW_SCHEDULED/RESCHEDULED/CANCELLED,
CANDIDATE_SELECTED). Plain functions taking a raw `db` session, matching
_queue_rejection_email's exact shape (deterministic_scoring_tasks.py) -
NOT a class requiring constructor injection, since every real caller here
is one of 5 already-large service classes (StageTransitionService,
PipelineTransitionService's 3 real callers, CampaignService) that have
nothing else to do with email notifications; widening all 5 constructors
just to inject one more collaborator would mean touching ~20 unrelated
test files for a dependency each of them only ever needs at the one
moment a candidate reaches SELECTED. Each of those classes already
exposes `.db` off an existing injected repo (e.g.
self.campaign_candidate_repo.db) - the same ad-hoc-repo-construction
pattern already used elsewhere in this codebase (campaign_candidate_
service.py's own CandidateCompositeScoreHistoryRepository(campaign_
candidate_repo.db), talent_pool_service.py's _enqueue_resume_embedding).

Queueing is deliberately independent of whatever transaction is calling
in: it commits its own EmailNotification row separately, wrapped in a
try/except that only ever logs - a failure to queue/dispatch a
notification must never roll back or block the real business transition
that already committed moments earlier (identical reasoning to
_queue_rejection_email). That guarantee has to cover EVERYTHING this
module does with its caller-supplied objects, including plain attribute
reads (campaign_candidate.candidate_id, schedule.id, ...) - those raised
AttributeError against several existing callers' bare test fixtures
during this feature's own build, precisely because an earlier version
evaluated them at the call site, one frame outside the try/except. The
4 public functions below are now thin wrappers that hand raw objects
straight to _queue_and_dispatch, which is the only place any attribute
access happens - see its own docstring.

Two dedup shapes, matching two different real-world repeat patterns:
- CANDIDATE_SELECTED is terminal, exactly like CANDIDATE_REJECTED -
  deduped per (campaign_candidate_id, trigger_event).
- INTERVIEW_SCHEDULED/RESCHEDULED/CANCELLED are NOT terminal - a
  candidate can have many interview rounds, and each round's schedule/
  reschedule/cancel is its own notification-worthy event. Deduped per
  (interview_schedule_id, trigger_event) instead - selected by whether
  a schedule was passed in at all.
"""


def _already_notified(notifications) -> bool:
    return any(n.status in (EmailNotificationStatus.QUEUED, EmailNotificationStatus.SENT) for n in notifications)


def _interview_email_context(schedule, interviewers: list) -> dict | None:
    """
    Shared placeholder set for all 3 interview trigger events, matching
    the seed templates' exact placeholder names - a template that doesn't
    use one of these keys simply ignores it (str.format only consumes
    the keys actually present in that template).

    Timezone-discrepancy fix: start_at is a real UTC instant now (see
    interview_schedule_service._combine_to_utc) - converted back to the
    round's own declared schedule.timezone before formatting, with the
    zone abbreviation appended (e.g. "2:00 PM IST"), rather than the
    previous bare strftime on a UTC value with no timezone indicator at
    all - the exact discrepancy between this email and the calendar
    invite (which a calendar client always localizes for its viewer).
    """
    if schedule.start_at is None:
        return None
    interviewer_names = ", ".join(i.name for i in interviewers) if interviewers else "the hiring team"
    local_start = schedule.start_at.astimezone(ZoneInfo(schedule.timezone))
    return {
        "interview_date": local_start.strftime("%B %d, %Y"),
        "interview_time": local_start.strftime("%I:%M %p %Z").lstrip("0"),
        "interview_mode": schedule.platform.value.title() if schedule.platform else "Not specified",
        "interviewer_name": interviewer_names,
    }


def _queue_and_dispatch(
    db, *, campaign_candidate, trigger_event: EmailTriggerEvent, schedule=None, interviewers=None,
) -> None:
    """
    Every attribute read on campaign_candidate/schedule, every dedup
    query, the template lookup, the create+commit, and the Celery
    dispatch all happen in this one try/except - nothing about this
    call is allowed to propagate into the real business transition that
    already committed moments earlier. schedule/interviewers present
    means this is one of the 3 per-round trigger events (dedup scoped to
    that round); absent means CANDIDATE_SELECTED (dedup scoped to the
    candidate, since it's terminal).
    """
    campaign_candidate_id = None
    try:
        candidate_id = campaign_candidate.candidate_id
        campaign_candidate_id = campaign_candidate.id
        interview_schedule_id = schedule.id if schedule is not None else None
        template_context = _interview_email_context(schedule, interviewers or []) if schedule is not None else None

        notification_repo = EmailNotificationRepository(db)
        if interview_schedule_id is not None:
            existing = notification_repo.get_by_interview_schedule_id_and_trigger_event(
                interview_schedule_id, trigger_event,
            )
        else:
            existing = notification_repo.get_by_campaign_candidate_id_and_trigger_event(
                campaign_candidate_id, trigger_event,
            )

        if _already_notified(existing):
            logger.info(
                "%s email already queued/sent - skipping duplicate | campaign_candidate_id=%s interview_schedule_id=%s",
                trigger_event.value, campaign_candidate_id, interview_schedule_id,
            )
            return

        template_repo = EmailTemplateRepository(db)
        template = template_repo.get_active_by_trigger_event(trigger_event)
        if template is None:
            logger.error(
                "No active %s email template configured - skipping | campaign_candidate_id=%s",
                trigger_event.value, campaign_candidate_id,
            )
            return

        notification = notification_repo.create(EmailNotification(
            candidate_id=candidate_id,
            campaign_candidate_id=campaign_candidate_id,
            interview_schedule_id=interview_schedule_id,
            trigger_event=trigger_event,
            template_id=template.id,
            status=EmailNotificationStatus.QUEUED,
            template_context=template_context,
        ))
        notification_repo.commit()

        send_candidate_email_task.apply_async(kwargs={"email_notification_id": str(notification.id)})
    except Exception:
        logger.exception(
            "Failed to queue %s email | campaign_candidate_id=%s", trigger_event.value, campaign_candidate_id,
        )


def queue_interview_scheduled_email(db, campaign_candidate, schedule, interviewers: list) -> None:
    _queue_and_dispatch(
        db, campaign_candidate=campaign_candidate, trigger_event=EmailTriggerEvent.INTERVIEW_SCHEDULED,
        schedule=schedule, interviewers=interviewers,
    )


def queue_interview_rescheduled_email(db, campaign_candidate, schedule, interviewers: list) -> None:
    _queue_and_dispatch(
        db, campaign_candidate=campaign_candidate, trigger_event=EmailTriggerEvent.INTERVIEW_RESCHEDULED,
        schedule=schedule, interviewers=interviewers,
    )


def queue_interview_cancelled_email(db, campaign_candidate, schedule, interviewers: list) -> None:
    _queue_and_dispatch(
        db, campaign_candidate=campaign_candidate, trigger_event=EmailTriggerEvent.INTERVIEW_CANCELLED,
        schedule=schedule, interviewers=interviewers,
    )


def queue_candidate_selected_email(db, campaign_candidate) -> None:
    _queue_and_dispatch(db, campaign_candidate=campaign_candidate, trigger_event=EmailTriggerEvent.CANDIDATE_SELECTED)
