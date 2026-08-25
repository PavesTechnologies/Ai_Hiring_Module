import logging

from app.core.config import settings
from app.core.feedback_token import sign_feedback_token
from app.models.email import EmailNotification, EmailNotificationStatus, EmailRecipientType, EmailTriggerEvent
from app.repositories.email_notification_repository import EmailNotificationRepository
from app.repositories.email_template_repository import EmailTemplateRepository
from app.tasks.email_tasks import send_candidate_email_task

logger = logging.getLogger(__name__)

"""
Epic 5 Step 4 - queue-and-dispatch for INTERVIEW_FEEDBACK_REQUESTED, the
first trigger event whose real recipient is an EXTERNAL_INTERVIEWER row,
not a candidate. Fired by InterviewFeedbackRequestSweepService's hourly
sweep once a round's end_at has passed - not synchronously off a user
action like every trigger in candidate_notification_emails.py.

Same shape as that module (plain function, everything - attribute reads,
dedup query, template lookup, create+commit, Celery dispatch - inside
one try/except; see its own docstring for why plain functions over a
class, and why the try/except has to cover attribute access too, not
just the DB calls).

Returns bool (True if an email was actually queued) rather than None
like candidate_notification_emails.py's functions - this one's callers
(the sweep, and the manual "Request Feedback" action below) genuinely
need an accurate count for their own summaries, the same way
detect_stalled_candidates reports how many alerts it actually raised,
not how many candidates it considered.
"""


def queue_interview_feedback_requested_email(db, campaign_candidate, schedule, interviewer) -> bool:
    try:
        notification_repo = EmailNotificationRepository(db)
        existing = notification_repo.get_by_interview_schedule_id_and_interviewer_id_and_trigger_event(
            schedule.id, interviewer.id, EmailTriggerEvent.INTERVIEW_FEEDBACK_REQUESTED,
        )
        if any(n.status in (EmailNotificationStatus.QUEUED, EmailNotificationStatus.SENT) for n in existing):
            logger.info(
                "INTERVIEW_FEEDBACK_REQUESTED email already queued/sent - skipping duplicate | "
                "interview_schedule_id=%s interviewer_id=%s", schedule.id, interviewer.id,
            )
            return False

        template_repo = EmailTemplateRepository(db)
        template = template_repo.get_active_by_trigger_event(EmailTriggerEvent.INTERVIEW_FEEDBACK_REQUESTED)
        if template is None:
            logger.error(
                "No active INTERVIEW_FEEDBACK_REQUESTED email template configured - skipping | "
                "interview_schedule_id=%s interviewer_id=%s", schedule.id, interviewer.id,
            )
            return False

        token = sign_feedback_token(schedule.id, interviewer.id)
        # Assumption, not confirmed against a real frontend page (none
        # exists yet as of this session) - see seed_email_templates.py's
        # own comment on this same [SEED] row.
        feedback_link = f"{settings.frontend_base_url}/interview-feedback/{token}"

        notification = notification_repo.create(EmailNotification(
            candidate_id=None,
            campaign_candidate_id=campaign_candidate.id,
            interview_schedule_id=schedule.id,
            interview_interviewer_id=interviewer.id,
            recipient_type=EmailRecipientType.EXTERNAL_INTERVIEWER,
            recipient_email=interviewer.email,
            recipient_name=interviewer.name,
            trigger_event=EmailTriggerEvent.INTERVIEW_FEEDBACK_REQUESTED,
            template_id=template.id,
            status=EmailNotificationStatus.QUEUED,
            template_context={"feedback_link": feedback_link, "recipient_name": interviewer.name},
        ))
        notification_repo.commit()

        send_candidate_email_task.apply_async(kwargs={"email_notification_id": str(notification.id)})
        return True
    except Exception:
        logger.exception(
            "Failed to queue INTERVIEW_FEEDBACK_REQUESTED email | interview_schedule_id=%s interviewer_id=%s",
            schedule.id if schedule is not None else None, interviewer.id if interviewer is not None else None,
        )
        return False


def queue_pending_feedback_requests_for_round(db, campaign_candidate, schedule, interviewers, interview_feedback_repo) -> int:
    """
    "Who still needs asking" (already gave feedback -> excluded) is real
    business logic, not just plumbing - shared by both
    InterviewFeedbackRequestSweepService's hourly sweep and
    InterviewScheduleService.request_feedback() (the manual trigger) so
    it can't silently drift between the two the way it easily could if
    each caller kept its own copy of this filter. The per-interviewer
    QUEUED/SENT dedup that actually prevents double-sending still lives
    one level down, inside queue_interview_feedback_requested_email
    itself - this function only decides who's even a candidate for that
    check.
    """
    already_given = {
        feedback.interviewer_id
        for feedback in interview_feedback_repo.get_by_interview_schedule_id(schedule.id)
    }
    queued_count = 0
    for interviewer in interviewers:
        if interviewer.id in already_given:
            continue
        if queue_interview_feedback_requested_email(db, campaign_candidate, schedule, interviewer):
            queued_count += 1
    return queued_count
