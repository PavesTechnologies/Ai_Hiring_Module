"""
Checks before every email (email plan, phase 2, 2026-11-13).

problems(db, notification) says why an email must not be sent - an empty list when it may. It runs:
- when an email is queued: automatic paths (interview, interviewer and feedback emails) skip it and log the
  codes; the manual sends (rejection, selection) refuse with the list (EmailRefused -> 422/409);
- in the send task, just before sending (at_send=True): the state may have changed since it was queued
  (an erasure request, a stage move, a cancelled round...). A refused email is marked FAILED with
  "BLOCKED: <codes>" and never retried.

Every email: a valid recipient address; for an email about a candidate, the candidate isn't erased, has no
erasure request and hasn't withdrawn consent (the latest consent record revoked or negative); an active
template that renders completely (every placeholder filled, none empty except the optional blocks); the
sender (when a person sends it) has a role allowed for this email and access to the campaign; not a duplicate.

Per type:
- interview scheduled: a date and time, at least one active interviewer, the INTERVIEW stage, an active campaign,
  and a candidate whose stage isn't locked (no rejection or selection email queued or sent; phase 4 adds the
  stored lock and its unlock);
- interview rescheduled: a real change from what the candidate was last told about the round (another time,
  or the round reinstated after a cancellation), on a round that isn't cancelled;
- interview cancelled: a round that was scheduled (has a time) and is cancelled now, not already notified;
- feedback request: an ended round, an interviewer on it who hasn't given feedback, and no request to them for
  that round within FEEDBACK_REQUEST_MIN_INTERVAL_HOURS;
- rejection only for REJECTED and selection only for SELECTED in that campaign; never both, never twice;
- interviewer invitation / rescheduled: an interviewer on a round that isn't cancelled (an invitation also
  needs the time); removed: an interviewer no longer on it; cancelled: a cancelled round.

Problem codes and numbers only go to logs and stored errors - never addresses or names.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from app.models.email import (
    QUEUED_OR_DELIVERED, EmailNotification, EmailRecipientType, EmailTemplate, EmailTriggerEvent as E,
)

logger = logging.getLogger(__name__)

FEEDBACK_INTERVAL_KEY = "FEEDBACK_REQUEST_MIN_INTERVAL_HOURS"
FEEDBACK_INTERVAL_DEFAULT_HOURS = 24
# Placeholders that may legitimately render empty (optional blocks); every other one must have a value.
MAY_BE_EMPTY = frozenset({"notes_block", "reason_block", "meeting_info", "suspended_note", "monitoring_link"})
_ADDRESS = re.compile(r"^[^@\s<>\"',;:()\[\]\\]+@[^@\s<>\"',;:()\[\]\\]+\.[^@\s<>\"',;:()\[\]\\]{2,}$")

INTERVIEW_CANDIDATE_EVENTS = frozenset({E.INTERVIEW_SCHEDULED, E.INTERVIEW_RESCHEDULED, E.INTERVIEW_CANCELLED})
INTERVIEWER_EVENTS = frozenset({E.INTERVIEW_INTERVIEWER_INVITATION, E.INTERVIEW_INTERVIEWER_RESCHEDULED,
                                E.INTERVIEW_INTERVIEWER_REMOVED, E.INTERVIEW_INTERVIEWER_CANCELLED,
                                E.INTERVIEW_FEEDBACK_REQUESTED})
OUTCOME_EVENTS = {E.CANDIDATE_REJECTED: "REJECTED", E.CANDIDATE_SELECTED: "SELECTED"}
# Who may send each email when a person sends it (the scheduled sweeps and internal alerts send as the system).
SENDER_ROLES = {
    E.CANDIDATE_REJECTED: {"HR_ADMIN", "HIRING_MANAGER"},
    E.CANDIDATE_SELECTED: {"HR_ADMIN", "HIRING_MANAGER"},
    **{event: {"HR_ADMIN", "HIRING_MANAGER", "RECRUITER"} for event in INTERVIEW_CANDIDATE_EVENTS | INTERVIEWER_EVENTS},
}


class Problem(NamedTuple):
    code: str
    message: str


class EmailRefused(Exception):
    def __init__(self, problems: list[Problem]):
        self.problems = problems
        super().__init__(", ".join(p.code for p in problems))

    @property
    def codes(self) -> list[str]:
        return [p.code for p in self.problems]

    def as_data(self) -> dict:
        return {"code": "EMAIL_REFUSED", "problems": [{"code": p.code, "message": p.message} for p in self.problems]}


def recipient_type(notification) -> EmailRecipientType:
    """The column defaults to CANDIDATE only when the row is saved; an unsaved row reads None."""
    return notification.recipient_type or EmailRecipientType.CANDIDATE


def require_sendable(db, notification, *, actor=None) -> None:
    found = problems(db, notification, actor=actor)
    if found:
        raise EmailRefused(found)


def valid_address(address: str | None) -> bool:
    return bool(address) and len(address) <= 254 and bool(_ADDRESS.match(address.strip()))


def empty_value_problems(template, context: dict) -> list[Problem]:
    """Placeholders the template uses whose value is empty (render() already refuses missing ones)."""
    from app.services.notifications.email_rendering import _fields
    empty = []
    for text in (getattr(template, "subject", ""), getattr(template, "body_template", "")):
        for _, field, _, _ in _fields(text or ""):
            if field and field not in MAY_BE_EMPTY and field in context and str(context[field] or "").strip() == "":
                empty.append(field)
    if not empty:
        return []
    names = ", ".join("{" + f + "}" for f in dict.fromkeys(empty))
    return [Problem("TEMPLATE_VALUE_EMPTY", f"The email would have empty values: {names}.")]


def outcome_email_sent(db, campaign_candidate_id, *, exclude_id=None) -> EmailNotification | None:
    """The rejection or selection email queued or sent for this campaign candidate, if any (the stage lock)."""
    if campaign_candidate_id is None:
        return None
    query = (db.query(EmailNotification)
             .filter(EmailNotification.campaign_candidate_id == campaign_candidate_id,
                     EmailNotification.trigger_event.in_(tuple(OUTCOME_EVENTS)),
                     EmailNotification.status.in_(QUEUED_OR_DELIVERED)))
    if exclude_id is not None:
        query = query.filter(EmailNotification.id != exclude_id)
    return query.order_by(EmailNotification.created_at).first()


def problems(db, notification, *, actor=None, at_send: bool = False, now: datetime | None = None) -> list[Problem]:
    """Why this email must not be sent (empty when it may). at_send: the send task renders it itself."""
    now = now or datetime.now(timezone.utc)
    event = notification.trigger_event
    found: list[Problem] = []
    ctx = _Context.load(db, notification)

    # --- every email ------------------------------------------------------------------------------
    address = ctx.recipient_address()
    if not valid_address(address):
        found.append(Problem("INVALID_RECIPIENT", "The recipient's email address is missing or not valid."))
    if ctx.candidate is not None:
        if ctx.candidate.erasure_completed_at is not None:
            found.append(Problem("CANDIDATE_ERASED", "The candidate's data has been erased."))
        elif ctx.candidate.erasure_requested_at is not None:
            found.append(Problem("ERASURE_REQUESTED", "The candidate has asked for their data to be erased."))
        if ctx.consent_withdrawn():
            found.append(Problem("CONSENT_WITHDRAWN", "The candidate has withdrawn their consent."))
    found += ctx.template_problems(render=not at_send)
    if actor is not None:
        found += _sender_problems(event, actor, ctx.campaign)
    if notification.dedup_key and _duplicate(db, notification):
        found.append(Problem("DUPLICATE", "This email is already queued or sent."))

    # --- per type ---------------------------------------------------------------------------------
    if event in OUTCOME_EVENTS:
        found += _outcome_problems(db, notification, ctx)
    elif event == E.INTERVIEW_SCHEDULED:
        found += _scheduled_problems(db, notification, ctx)
    elif event == E.INTERVIEW_RESCHEDULED:
        found += _rescheduled_problems(db, notification, ctx)
    elif event == E.INTERVIEW_CANCELLED:
        found += _cancelled_problems(ctx)
    elif event == E.INTERVIEW_FEEDBACK_REQUESTED:
        found += _feedback_problems(db, notification, ctx, now)
    elif event in INTERVIEWER_EVENTS:
        found += _interviewer_problems(event, ctx)
    return list(dict.fromkeys(found))


# --- loading -------------------------------------------------------------------------------------

class _Context:
    """What the checks read for one email, loaded once."""

    def __init__(self, db, notification):
        self.db, self.notification = db, notification
        self.campaign_candidate = self.campaign = self.candidate = self.schedule = self.interviewer = None
        self._encryption = None

    @classmethod
    def load(cls, db, notification) -> "_Context":
        from app.models.campaigns import HiringCampaign
        from app.models.candidates import Candidate
        from app.models.interview import InterviewInterviewer, InterviewSchedule
        from app.models.pipeline import CampaignCandidate
        ctx = cls(db, notification)
        if notification.campaign_candidate_id is not None:
            ctx.campaign_candidate = db.get(CampaignCandidate, notification.campaign_candidate_id)
        if ctx.campaign_candidate is not None:
            ctx.campaign = db.get(HiringCampaign, ctx.campaign_candidate.campaign_id)
        candidate_id = notification.candidate_id or getattr(ctx.campaign_candidate, "candidate_id", None)
        if candidate_id is not None:
            ctx.candidate = db.get(Candidate, candidate_id)
        if notification.interview_schedule_id is not None:
            ctx.schedule = db.get(InterviewSchedule, notification.interview_schedule_id)
        if notification.interview_interviewer_id is not None:
            ctx.interviewer = db.get(InterviewInterviewer, notification.interview_interviewer_id)
        return ctx

    def _decrypt(self, ciphertext, key_id) -> str | None:
        if ciphertext is None or key_id is None:
            return None
        if self._encryption is None:
            from app.core.encryption_service import EncryptionService
            from app.repositories.encryption_key_repository import EncryptionKeyRepository
            self._encryption = EncryptionService(EncryptionKeyRepository(self.db))
        try:
            return self._encryption.decrypt(ciphertext, key_id)
        except Exception:
            return None

    def recipient_address(self) -> str | None:
        if recipient_type(self.notification) == EmailRecipientType.CANDIDATE:
            if self.candidate is None:
                return None
            return self._decrypt(self.candidate.email_encrypted, self.candidate.encryption_key_id)
        return self.notification.recipient_email

    def consent_withdrawn(self) -> bool:
        from app.models.compliance import CandidateConsent
        latest = (self.db.query(CandidateConsent).filter(CandidateConsent.candidate_id == self.candidate.id)
                  .order_by(CandidateConsent.consent_timestamp.desc()).first())
        return latest is not None and (latest.revoked_at is not None or latest.consent_given is False)

    def template_problems(self, *, render: bool) -> list[Problem]:
        from app.repositories.email_template_repository import EmailTemplateRepository
        event = self.notification.trigger_event
        template = (self.db.get(EmailTemplate, self.notification.template_id)
                    if self.notification.template_id is not None else None)
        active = EmailTemplateRepository(self.db).get_active_by_trigger_event(event)
        if active is None:
            return [Problem("NO_ACTIVE_TEMPLATE", "No active email template is set up for this email.")]
        if template is not None and not template.is_active:
            return [Problem("TEMPLATE_INACTIVE", "The email's template was replaced since it was prepared.")]
        if not render:
            return []
        from app.services.notifications.email_rendering import TemplateRenderError, render as render_text
        context = self.render_context()
        try:
            render_text(active.subject, context)
            render_text(active.body_template, context)
        except TemplateRenderError:
            return [Problem("TEMPLATE_INCOMPLETE", "The template has a placeholder this email can't fill.")]
        return empty_value_problems(active, context)

    def render_context(self) -> dict:
        """The values the send task renders with (email_tasks.send_candidate_email_task)."""
        from app.models.jd.job_descriptions import JobDescription
        job_title = "the position you applied for"
        if self.campaign is not None:
            jd = self.db.get(JobDescription, self.campaign.jd_id)
            if jd is not None:
                job_title = jd.title
        name = None
        if self.candidate is not None:
            name = self._decrypt(self.candidate.full_name_encrypted, self.candidate.encryption_key_id)
        if recipient_type(self.notification) != EmailRecipientType.CANDIDATE and name is None:
            name = "the candidate"
        return {"candidate_name": name, "job_title": job_title, **(self.notification.template_context or {})}


# --- checks --------------------------------------------------------------------------------------

def _sender_problems(event, actor, campaign) -> list[Problem]:
    from app.services.campaign_access import can_access_campaign
    roles = set(getattr(actor, "roles", ()) or ())
    allowed = SENDER_ROLES.get(event)
    if allowed is None or not roles & allowed:
        return [Problem("SENDER_NOT_ALLOWED", "Your role can't send this email.")]
    if campaign is not None and not can_access_campaign(actor, campaign):
        return [Problem("NO_CAMPAIGN_ACCESS", "You don't have access to this candidate's campaign.")]
    return []


def _duplicate(db, notification) -> bool:
    query = (db.query(EmailNotification.id)
             .filter(EmailNotification.dedup_key == notification.dedup_key,
                     EmailNotification.status.in_(QUEUED_OR_DELIVERED)))
    if notification.id is not None:
        query = query.filter(EmailNotification.id != notification.id)
    return query.first() is not None


def _sent_on(row) -> str:
    when = row.sent_at or row.created_at
    return f" on {when:%d %b %Y}" if when else ""


def _outcome_problems(db, notification, ctx) -> list[Problem]:
    event = notification.trigger_event
    if ctx.campaign_candidate is None:
        return [Problem("NOT_IN_CAMPAIGN", "The candidate isn't in this campaign.")]
    found = []
    stage = getattr(ctx.campaign_candidate.pipeline_stage, "value", ctx.campaign_candidate.pipeline_stage)
    if stage != OUTCOME_EVENTS[event]:
        word = "rejected" if event == E.CANDIDATE_REJECTED else "selected"
        found.append(Problem("WRONG_STAGE", f"The candidate isn't {word} in this campaign."))
    earlier = outcome_email_sent(db, ctx.campaign_candidate.id, exclude_id=notification.id)
    if earlier is not None:
        if earlier.trigger_event == event:
            found.append(Problem("ALREADY_SENT", f"This email was already sent{_sent_on(earlier)}; it's never sent twice."))
        else:
            other = "rejection" if earlier.trigger_event == E.CANDIDATE_REJECTED else "selection"
            found.append(Problem("OTHER_OUTCOME_SENT", f"A {other} email was already sent{_sent_on(earlier)}."))
    return found


def _active_interviewers(db, schedule) -> list:
    from app.models.interview import InterviewInterviewer
    return (db.query(InterviewInterviewer)
            .filter(InterviewInterviewer.interview_id == schedule.id, InterviewInterviewer.is_active.is_(True)).all())


def _status(schedule) -> str | None:
    return getattr(schedule.status, "value", schedule.status) if schedule is not None else None


def _scheduled_problems(db, notification, ctx) -> list[Problem]:
    found = []
    schedule = ctx.schedule
    if schedule is None:
        return [Problem("NO_ROUND", "The interview round no longer exists.")]
    if schedule.start_at is None or schedule.end_at is None:
        found.append(Problem("NO_DATE_TIME", "The round has no date and time."))
    if not _active_interviewers(db, schedule):
        found.append(Problem("NO_INTERVIEWERS", "The round has no interviewers."))
    if _status(schedule) == "CANCELLED":
        found.append(Problem("ROUND_CANCELLED", "The round is cancelled."))
    if ctx.campaign_candidate is not None:
        stage = getattr(ctx.campaign_candidate.pipeline_stage, "value", ctx.campaign_candidate.pipeline_stage)
        if stage != "INTERVIEW":
            found.append(Problem("NOT_IN_INTERVIEW_STAGE", "The candidate isn't in the Interview stage."))
        if outcome_email_sent(db, ctx.campaign_candidate.id) is not None:
            found.append(Problem("CANDIDATE_LOCKED", "A rejection or selection email was already sent to the candidate."))
    if ctx.campaign is not None and getattr(ctx.campaign.status, "value", ctx.campaign.status) != "ACTIVE":
        found.append(Problem("CAMPAIGN_NOT_ACTIVE", "The campaign isn't active."))
    return found


def _last_candidate_round_email(db, notification):
    return (db.query(EmailNotification)
            .filter(EmailNotification.interview_schedule_id == notification.interview_schedule_id,
                    EmailNotification.recipient_type == EmailRecipientType.CANDIDATE,
                    EmailNotification.trigger_event.in_(tuple(INTERVIEW_CANDIDATE_EVENTS)),
                    EmailNotification.status.in_(QUEUED_OR_DELIVERED),
                    *([EmailNotification.id != notification.id] if notification.id is not None else []))
            .order_by(EmailNotification.created_at.desc()).first())


def _when(context: dict | None) -> tuple:
    context = context or {}
    return context.get("interview_date"), context.get("interview_time")


def _rescheduled_problems(db, notification, ctx) -> list[Problem]:
    if ctx.schedule is None:
        return [Problem("NO_ROUND", "The interview round no longer exists.")]
    found = []
    if _status(ctx.schedule) == "CANCELLED":
        found.append(Problem("ROUND_CANCELLED", "The round is cancelled."))
    if ctx.schedule.start_at is None:
        found.append(Problem("NO_DATE_TIME", "The round has no date and time."))
    last = _last_candidate_round_email(db, notification)
    if (last is not None and last.trigger_event != E.INTERVIEW_CANCELLED
            and _when(last.template_context) == _when(notification.template_context)):
        found.append(Problem("NO_TIME_CHANGE", "The date and time are the same as the candidate was last told."))
    return found


def _cancelled_problems(ctx) -> list[Problem]:
    if ctx.schedule is None:
        return [Problem("NO_ROUND", "The interview round no longer exists.")]
    found = []
    if ctx.schedule.start_at is None:
        found.append(Problem("NO_DATE_TIME", "The round was never scheduled with a date and time."))
    if _status(ctx.schedule) != "CANCELLED":
        found.append(Problem("ROUND_NOT_CANCELLED", "The round isn't cancelled."))
    return found


def _feedback_interval_hours(db) -> float:
    from app.repositories.config_repository import ConfigRepository
    try:
        raw = ConfigRepository(db).get_configs_by_keys([FEEDBACK_INTERVAL_KEY]).get(FEEDBACK_INTERVAL_KEY)
        return float(raw) if raw is not None else FEEDBACK_INTERVAL_DEFAULT_HOURS
    except Exception:
        return FEEDBACK_INTERVAL_DEFAULT_HOURS


def _feedback_problems(db, notification, ctx, now) -> list[Problem]:
    from app.models.interview import InterviewFeedback
    schedule, interviewer = ctx.schedule, ctx.interviewer
    if schedule is None or interviewer is None:
        return [Problem("NO_ROUND", "The interview round or interviewer no longer exists.")]
    found = []
    status = _status(schedule)
    ended = status == "COMPLETED" or (schedule.end_at is not None and schedule.end_at <= now)
    if status == "CANCELLED" or not ended:
        found.append(Problem("ROUND_NOT_ENDED", "The round hasn't ended."))
    if interviewer.interview_id != schedule.id or not interviewer.is_active:
        found.append(Problem("INTERVIEWER_NOT_ON_ROUND", "The interviewer isn't on this round."))
    given = (db.query(InterviewFeedback.id)
             .filter(InterviewFeedback.interview_schedule_id == schedule.id,
                     InterviewFeedback.interviewer_id == interviewer.id).first())
    if given is not None:
        found.append(Problem("FEEDBACK_GIVEN", "The interviewer has already given feedback."))
    hours = _feedback_interval_hours(db)
    recent = (db.query(EmailNotification.id)
              .filter(EmailNotification.interview_schedule_id == schedule.id,
                      EmailNotification.interview_interviewer_id == interviewer.id,
                      EmailNotification.trigger_event == E.INTERVIEW_FEEDBACK_REQUESTED,
                      EmailNotification.status.in_(QUEUED_OR_DELIVERED),
                      EmailNotification.created_at >= now - timedelta(hours=hours),
                      *([EmailNotification.id != notification.id] if notification.id is not None else []))
              .first())
    if recent is not None:
        found.append(Problem("RECENTLY_REQUESTED", f"Feedback was already requested in the last {hours:g} hours."))
    return found


def _interviewer_problems(event, ctx) -> list[Problem]:
    schedule, interviewer = ctx.schedule, ctx.interviewer
    if schedule is None or interviewer is None:
        return [Problem("NO_ROUND", "The interview round or interviewer no longer exists.")]
    on_round = interviewer.interview_id == schedule.id and interviewer.is_active
    cancelled = _status(schedule) == "CANCELLED"
    found = []
    if event == E.INTERVIEW_INTERVIEWER_REMOVED:
        if on_round:
            found.append(Problem("INTERVIEWER_STILL_ON_ROUND", "The interviewer is still on this round."))
        return found
    if event == E.INTERVIEW_INTERVIEWER_CANCELLED:
        if not cancelled:
            found.append(Problem("ROUND_NOT_CANCELLED", "The round isn't cancelled."))
        return found
    if not on_round:
        found.append(Problem("INTERVIEWER_NOT_ON_ROUND", "The interviewer isn't on this round."))
    if cancelled:
        found.append(Problem("ROUND_CANCELLED", "The round is cancelled."))
    if event == E.INTERVIEW_INTERVIEWER_INVITATION and schedule.start_at is None:
        found.append(Problem("NO_DATE_TIME", "The round has no date and time."))
    return found
