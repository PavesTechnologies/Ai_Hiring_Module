"""
Stage lock (email plan, phase 4, 2026-11-16).

Once a rejection or selection email is sent to a candidate, their stage in that campaign is locked: no stage moves,
and re-parse / re-score can't move them. The send task sets the lock (lock_after_send) in the same transaction
that records the email as SENT (or LOGGED, or accepted by the local Mailpit). From the moment Send is confirmed
until the send finishes, the rejection or selection email that is queued or sending already blocks moves ("being
sent"); if that send fails, no lock is set.

A database trigger (a9c1e3f5b7d0) refuses any stage change on a locked row, so a path missed in the code still
can't move the candidate. An HR admin can unlock with a STAGE_UNLOCK reason code (audited STAGE_UNLOCKED). Unlocking
doesn't allow the email to be sent again (never twice - send_policy).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from app.exceptions.campaign_exceptions import CampaignException
from app.models.email import EmailNotification, EmailNotificationStatus, EmailTriggerEvent

logger = logging.getLogger(__name__)

LOCK_REASONS = {EmailTriggerEvent.CANDIDATE_REJECTED: "REJECTION_EMAIL_SENT",
                EmailTriggerEvent.CANDIDATE_SELECTED: "SELECTION_EMAIL_SENT"}
LOCKING_STATUSES = (EmailNotificationStatus.SENT, EmailNotificationStatus.LOGGED)
PENDING_STATUSES = (EmailNotificationStatus.QUEUED, EmailNotificationStatus.SENDING)


@dataclass(frozen=True)
class LockState:
    locked: bool
    reason: str | None = None            # REJECTION_EMAIL_SENT / SELECTION_EMAIL_SENT, or *_PENDING while sending
    since: datetime | None = None
    notification_id: object = None

    @property
    def message(self) -> str:
        if not self.locked:
            return ""
        word = "rejection" if (self.reason or "").startswith("REJECTION") else "selection"
        if (self.reason or "").endswith("_PENDING"):
            return f"A {word} email to this candidate is being sent, so their stage can't change."
        when = f" on {self.since:%d %b %Y}" if self.since else ""
        return (f"A {word} email was sent to this candidate{when}, so their stage is locked. "
                "An HR admin can unlock it with a reason.")


def _pending_outcome_email(db, campaign_candidate_id):
    return (db.query(EmailNotification)
            .filter(EmailNotification.campaign_candidate_id == campaign_candidate_id,
                    EmailNotification.trigger_event.in_(tuple(LOCK_REASONS)),
                    EmailNotification.status.in_(PENDING_STATUSES))
            .order_by(EmailNotification.created_at.desc()).first())


def lock_state(db, campaign_candidate) -> LockState:
    """The stored lock, or a rejection/selection email being sent right now."""
    locked_at = getattr(campaign_candidate, "stage_locked_at", None)
    if isinstance(locked_at, datetime):
        return LockState(True, campaign_candidate.stage_lock_reason, locked_at,
                         campaign_candidate.stage_lock_notification_id)
    try:
        pending = _pending_outcome_email(db, campaign_candidate.id)
    except Exception:
        return LockState(False)
    if isinstance(pending, EmailNotification):
        return LockState(True, LOCK_REASONS[pending.trigger_event].replace("_SENT", "_PENDING"), None, pending.id)
    return LockState(False)


def refuse_if_locked(db, campaign_candidate) -> None:
    """For a person's stage move: 409 with the reason when the stage is locked."""
    state = lock_state(db, campaign_candidate)
    if state.locked:
        raise CampaignException(state.message, 409, data={
            "code": "STAGE_LOCKED", "lock_reason": state.reason,
            "locked_at": state.since.isoformat() if state.since else None})


def lock_after_send(db, notification) -> bool:
    """Called by the send task, before its commit, when a rejection/selection email is recorded as sent."""
    if notification.trigger_event not in LOCK_REASONS or notification.campaign_candidate_id is None:
        return False
    from app.models.pipeline import CampaignCandidate
    cc = db.get(CampaignCandidate, notification.campaign_candidate_id)
    if cc is None or cc.stage_locked_at is not None:
        return False
    cc.stage_locked_at = datetime.now(timezone.utc)
    cc.stage_lock_reason = LOCK_REASONS[notification.trigger_event]
    cc.stage_lock_notification_id = notification.id
    return True


def unlock_reason_codes(db, *, active_only: bool = True) -> dict[str, str]:
    from app.models.outcome_overrides import OverrideReasonCode
    rows = (db.query(OverrideReasonCode).filter(OverrideReasonCode.kind == "STAGE_UNLOCK")
            .order_by(OverrideReasonCode.sort_order, OverrideReasonCode.code).all())
    return {r.code: r.label for r in rows if r.is_active or not active_only}


def unlock(db, campaign_candidate_id, *, reason_code: str, actor, audit_service) -> dict:
    """HR admin only (the route): clears the lock, audited STAGE_UNLOCKED with the reason code and the email's id."""
    from app.enums.constants import ActionType, EntityType
    from app.middleware.rbac import authorising_role
    from app.models.pipeline import CampaignCandidate
    cc = db.get(CampaignCandidate, campaign_candidate_id)
    if cc is None:
        raise CampaignException("Campaign candidate not found.", 404)
    codes = unlock_reason_codes(db)
    if reason_code not in codes:
        raise CampaignException("Choose a reason for unlocking.", 422,
                                data={"code": "UNLOCK_REASON_REQUIRED", "reasons": list(codes)})
    if cc.stage_locked_at is None:
        raise CampaignException("This candidate's stage isn't locked.", 409, data={"code": "NOT_LOCKED"})
    before = {"lock_reason": cc.stage_lock_reason,
              "notification_id": str(cc.stage_lock_notification_id) if cc.stage_lock_notification_id else None,
              "locked_at": cc.stage_locked_at.isoformat()}
    cc.stage_locked_at = None
    cc.stage_lock_reason = None
    cc.stage_lock_notification_id = None
    try:
        audit_service.log(
            actor_id=getattr(actor, "user_id", None), actor_role=authorising_role(actor, ("HR_ADMIN",)),
            action_type=ActionType.STAGE_UNLOCKED.value, entity_type=EntityType.CAMPAIGN_CANDIDATE.value,
            entity_id=cc.id, campaign_id=cc.campaign_id,
            details={"reason_code": reason_code, "stage": cc.pipeline_stage.value, **before})
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"campaign_candidate_id": str(cc.id), "stage": cc.pipeline_stage.value, "unlocked": True}
