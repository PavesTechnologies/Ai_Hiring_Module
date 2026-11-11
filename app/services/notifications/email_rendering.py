"""
Email template placeholders and the renderer (settings phase 1, 2026-11-11).

PLACEHOLDERS lists, per trigger event, the values its sending code supplies (checked against the senders:
candidate_notification_emails, candidate_rejection_email_service, interview_feedback_request_emails,
interview_interviewer_lifecycle_emails, embedding_health_tasks, email_health_tasks; email_tasks adds
candidate_name and job_title to every send). A template may use only those, as plain {name} fields.

render() replaces str.format: str.format also evaluates {x.attr} and {x[0]} and applies format specs, so an
edited template could reach object attributes. render() accepts plain names only and raises
TemplateRenderError for anything else or a value the send doesn't have - the send then fails (and is
dead-lettered) as before, instead of producing a wrong email. Literal braces are written {{ and }}, as before.
"""
from string import Formatter

from app.models.email import EmailTriggerEvent as E

_BASE = ("candidate_name", "job_title")
_INTERVIEW = ("interview_date", "interview_time", "interview_mode")

PLACEHOLDERS: dict[E, tuple[str, ...]] = {
    E.CANDIDATE_REJECTED: _BASE,
    E.CANDIDATE_SELECTED: _BASE,
    E.INTERVIEW_SCHEDULED: _BASE + _INTERVIEW + ("interviewer_name", "meeting_info"),
    E.INTERVIEW_RESCHEDULED: _BASE + _INTERVIEW + ("interviewer_name", "meeting_info"),
    E.INTERVIEW_CANCELLED: _BASE + _INTERVIEW + ("interviewer_name", "meeting_info"),
    E.INTERVIEW_FEEDBACK_REQUESTED: _BASE + ("recipient_name", "feedback_link"),
    E.INTERVIEW_INTERVIEWER_INVITATION: _BASE + ("recipient_name",) + _INTERVIEW + ("notes_block",),
    E.INTERVIEW_INTERVIEWER_RESCHEDULED: _BASE + ("recipient_name",) + _INTERVIEW,
    E.INTERVIEW_INTERVIEWER_REMOVED: _BASE + ("recipient_name",),
    E.INTERVIEW_INTERVIEWER_CANCELLED: _BASE + ("recipient_name",) + _INTERVIEW + ("reason_block",),
    E.EMBEDDING_HEALTH_ALERT: ("recipient_name", "campaign_name", "affected_count", "total_screening_count",
                               "failure_percentage", "circuit_breaker_state", "monitoring_link", "suspended_note"),
    E.EMAIL_SEND_STUCK_ALERT: ("recipient_name", "stuck_count", "stuck_after_minutes", "oldest_minutes",
                               "by_trigger", "monitoring_link"),
}
# No code sends this event yet (D11); its template is shown but can't be edited.
NOT_SENT = frozenset({E.UPLOAD_PERMANENTLY_FAILED})

# Made-up values for the preview - never a real candidate.
SAMPLE_VALUES = {
    "candidate_name": "Alex Sample", "job_title": "Senior Data Engineer", "recipient_name": "Sam Interviewer",
    "interview_date": "March 14, 2027", "interview_time": "2:30 PM IST", "interview_mode": "Teams",
    "interviewer_name": "Sam Interviewer, Priya Lead", "meeting_info": "Join link: https://example.invalid/meet",
    "notes_block": "Notes from the scheduler:\nPlease focus on system design.",
    "reason_block": "Reason given:\nThe candidate asked to reschedule.",
    "feedback_link": "https://example.invalid/feedback/sample", "campaign_name": "Sample campaign",
    "affected_count": 6, "total_screening_count": 25, "failure_percentage": "24.00",
    "circuit_breaker_state": "CLOSED", "monitoring_link": "https://example.invalid/monitoring",
    "suspended_note": "", "stuck_count": 3, "stuck_after_minutes": 30, "oldest_minutes": 95,
    "by_trigger": "CANDIDATE_REJECTED (2), INTERVIEW_SCHEDULED (1)",
}

SUBJECT_MAX = 255


class TemplateRenderError(ValueError):
    pass


def _fields(text: str):
    """(literal, field, spec, conversion) parts; raises TemplateRenderError on unbalanced braces."""
    try:
        return list(Formatter().parse(text or ""))
    except ValueError as exc:
        raise TemplateRenderError(f"Unbalanced braces: {exc}. Write a literal brace as {{{{ or }}}}.") from exc


def template_problems(text: str, allowed) -> list[str]:
    """Why this text can't be used with these placeholders (empty list when it can)."""
    try:
        parts = _fields(text)
    except TemplateRenderError as exc:
        return [str(exc)]
    problems = []
    for _, field, spec, conversion in parts:
        if field is None:
            continue
        if not field.isidentifier():
            problems.append(f"{{{field}}}: only plain names are allowed (no dots, brackets or positions).")
        elif spec or conversion:
            problems.append(f"{{{field}}}: formatting after the name is not allowed.")
        elif field not in allowed:
            problems.append(f"{{{field}}} isn't supplied for this email. Available: "
                            + ", ".join("{" + a + "}" for a in allowed) + ".")
    return list(dict.fromkeys(problems))


def render(text: str, context: dict) -> str:
    out = []
    for literal, field, spec, conversion in _fields(text):
        out.append(literal)
        if field is None:
            continue
        if not field.isidentifier() or spec or conversion:
            raise TemplateRenderError(f"Unsupported placeholder {{{field}}}.")
        if field not in context:
            raise TemplateRenderError(f"No value for {{{field}}}.")
        value = context[field]
        out.append("" if value is None else str(value))
    return "".join(out)
