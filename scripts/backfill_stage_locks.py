"""
Lock the stage of candidates whose rejection or selection email was sent before the stage lock existed
(email plan phase 4, a9c1e3f5b7d0). Approved for dev on 2026-11-16; production starts on a fresh database, where
the send task sets every lock itself, so there is nothing to backfill there.

    python -m scripts.backfill_stage_locks            # dry run: counts only
    python -m scripts.backfill_stage_locks --apply    # writes, one transaction

A candidate is locked only when:
  - their latest rejection/selection email for that campaign is SENT or LOGGED;
  - they are still in the matching stage (REJECTED for a rejection, SELECTED for a selection);
  - they aren't locked already;
  - and no HR admin has unlocked them since that email (an unlock was a decision - it isn't undone).
The lock gets the email's sent time, the reason the send task would have given, and the email's id - the same
three values lock_after_send writes. Everyone else is left as is and counted with the reason. Prints ids and counts
only, never names or addresses.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass

from app.models.email import EmailNotification
from app.models.pipeline import CampaignCandidate, PipelineStage
from app.services.campaign.stage_lock import LOCK_REASONS, LOCKING_STATUSES

MATCHING_STAGE = {"REJECTION_EMAIL_SENT": PipelineStage.REJECTED, "SELECTION_EMAIL_SENT": PipelineStage.SELECTED}


@dataclass(frozen=True)
class Decision:
    campaign_candidate_id: object
    notification_id: object
    outcome: str        # LOCK / ALREADY_LOCKED / STAGE_MOVED / UNLOCKED_SINCE


def plan(db) -> list[Decision]:
    """One decision per campaign candidate with a sent rejection or selection email."""
    from app.models.compliance import AuditLog
    emails = (db.query(EmailNotification)
              .filter(EmailNotification.trigger_event.in_(tuple(LOCK_REASONS)),
                      EmailNotification.status.in_(LOCKING_STATUSES),
                      EmailNotification.campaign_candidate_id.isnot(None))
              .order_by(EmailNotification.campaign_candidate_id,
                        EmailNotification.sent_at.desc().nulls_last(), EmailNotification.created_at.desc())
              .all())
    latest: dict = {}
    for email in emails:
        latest.setdefault(email.campaign_candidate_id, email)
    decisions = []
    for cc_id, email in latest.items():
        cc = db.get(CampaignCandidate, cc_id)
        if cc is None:
            continue
        reason = LOCK_REASONS[email.trigger_event]
        sent = email.sent_at or email.created_at
        unlocked_since = (db.query(AuditLog.id)
                          .filter(AuditLog.entity_id == cc.id, AuditLog.action_type == "STAGE_UNLOCKED",
                                  AuditLog.created_at >= sent).first())
        if cc.stage_locked_at is not None:
            outcome = "ALREADY_LOCKED"
        elif cc.pipeline_stage != MATCHING_STAGE[reason]:
            outcome = "STAGE_MOVED"
        elif unlocked_since is not None:
            outcome = "UNLOCKED_SINCE"
        else:
            outcome = "LOCK"
        decisions.append(Decision(cc.id, email.id, outcome))
    return decisions


def apply(db, decisions: list[Decision]) -> int:
    """Writes the locks for the LOCK decisions (no commit). Returns how many were written."""
    written = 0
    for decision in decisions:
        if decision.outcome != "LOCK":
            continue
        cc = db.get(CampaignCandidate, decision.campaign_candidate_id)
        email = db.get(EmailNotification, decision.notification_id)
        cc.stage_locked_at = email.sent_at or email.created_at
        cc.stage_lock_reason = LOCK_REASONS[email.trigger_event]
        cc.stage_lock_notification_id = email.id
        written += 1
    db.flush()
    return written


def main(argv=None) -> int:
    from app.db.session import SessionLocal
    parser = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the locks (default: dry run)")
    args = parser.parse_args(argv)
    db = SessionLocal()
    try:
        decisions = plan(db)
        for d in decisions:
            print(f"campaign candidate {d.campaign_candidate_id} | email {d.notification_id} | {d.outcome}")
        written = apply(db, decisions) if args.apply else 0
        if args.apply:
            db.commit()
        else:
            db.rollback()
        counts = Counter(d.outcome for d in decisions)
        print(f"sent outcome emails {len(decisions)} | to lock {counts['LOCK']} | already locked "
              f"{counts['ALREADY_LOCKED']} | stage moved since {counts['STAGE_MOVED']} | unlocked since "
              f"{counts['UNLOCKED_SINCE']} | {f'written {written}' if args.apply else 'dry run - nothing written'}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
