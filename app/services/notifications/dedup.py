"""
What makes two email notifications "the same email" (review item 7).

Every automatic queuer stamps its row with one of these keys, and a partial unique index
(uq_email_notifications_dedup_key: unique while status <> 'FAILED') lets only one live row hold it -
so two schedulers, two overlapping sweeps, or a retried request can never both queue the email. The
read-before-insert checks the queuers already do stay as the fast path; the index is the guarantee.

Each key has exactly the shape of that trigger's existing dedup rule:
- terminal candidate events (CANDIDATE_REJECTED, CANDIDATE_SELECTED): once per campaign candidate;
- per-round candidate events (INTERVIEW_SCHEDULED / RESCHEDULED / CANCELLED): once per interview round;
- interviewer events (invitation, rescheduled, removed, cancelled, feedback request): once per round
  and interviewer.

A manual re-send (allow_resend=True) carries no key, so it is never blocked; nor do internal-user alerts,
which have their own cadence.

Review item 17: a round can be rescheduled or cancelled (and reinstated, and cancelled) more than once,
and every time is news to the candidate and the interviewers. Those keys also carry the interview
history event that caused them (interview_event_id), so the second reschedule is not mistaken for the
first.
"""
from uuid import UUID

from app.models.email import EmailTriggerEvent

# Triggers that can happen more than once to the same round -> the history event type behind them.
_REPEATABLE_ROUND_EVENTS = {
    EmailTriggerEvent.INTERVIEW_RESCHEDULED: "RESCHEDULED",
    EmailTriggerEvent.INTERVIEW_INTERVIEWER_RESCHEDULED: "RESCHEDULED",
    EmailTriggerEvent.INTERVIEW_CANCELLED: "CANCELLED",
    EmailTriggerEvent.INTERVIEW_INTERVIEWER_CANCELLED: "CANCELLED",
}


def interview_event_id(db, interview_schedule_id: UUID, trigger_event: EmailTriggerEvent) -> UUID | None:
    """
    For a repeatable round trigger: the id of the round's latest history event of the matching type
    (reschedule() and cancel() write it in the same commit, before queueing the emails). None for other
    triggers, or if there is no such event.
    """
    event_type = _REPEATABLE_ROUND_EVENTS.get(trigger_event)
    if event_type is None:
        return None
    from app.models.interview import InterviewHistoryEventType, InterviewScheduleHistory
    entry = (
        db.query(InterviewScheduleHistory)
        .filter(InterviewScheduleHistory.interview_id == interview_schedule_id,
                InterviewScheduleHistory.event_type == InterviewHistoryEventType(event_type))
        .order_by(InterviewScheduleHistory.changed_at.desc(), InterviewScheduleHistory.id.desc())
        .first()
    )
    event_id = getattr(entry, "id", None)
    return event_id if isinstance(event_id, UUID) else None


def notification_dedup_key(
    trigger_event: EmailTriggerEvent,
    *,
    campaign_candidate_id: UUID | None = None,
    interview_schedule_id: UUID | None = None,
    interviewer_id: UUID | None = None,
    event_id: UUID | None = None,
) -> str:
    """
    trigger|cc=...|round=...|interviewer=...|event=... with only the parts given, in that order.
    event_id is one interview history event (a reschedule or a cancellation), for triggers that can
    happen more than once to the same round.
    """
    parts = [trigger_event.value]
    for name, value in (("cc", campaign_candidate_id), ("round", interview_schedule_id),
                        ("interviewer", interviewer_id), ("event", event_id)):
        if value is not None:
            parts.append(f"{name}={value}")
    if len(parts) == 1:
        raise ValueError(f"A {trigger_event.value} dedup key needs at least one id.")
    return "|".join(parts)
