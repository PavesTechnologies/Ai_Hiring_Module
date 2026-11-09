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
"""
from uuid import UUID

from app.models.email import EmailTriggerEvent


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
