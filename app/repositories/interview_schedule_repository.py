from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.interview import (
    InterviewHistoryEventType,
    InterviewInterviewer,
    InterviewSchedule,
    InterviewScheduleHistory,
    InterviewStatus,
)
from app.models.pipeline import CampaignCandidate

_ACTIVE_STATUSES = (InterviewStatus.PENDING, InterviewStatus.SCHEDULED, InterviewStatus.RESCHEDULED)


class InterviewScheduleRepository:

    def __init__(self, db: Session):
        self.db = db

    def get_by_campaign_candidate_id(self, campaign_candidate_id: UUID) -> InterviewSchedule | None:
        """
        Multi-round follow-up: used only by get_or_create_pending's "does
        this candidate have any row at all" check below - that check
        never cares which round it gets back, only whether one exists, so
        an explicit (if otherwise arbitrary) ordering is enough to make
        the result deterministic. schedule()/the GET endpoint use
        get_latest_by_campaign_candidate_id/get_all_by_campaign_candidate_id
        below instead - both need to reason about *which* round(s), which
        this method deliberately does not.
        """
        stmt = (
            select(InterviewSchedule)
            .where(InterviewSchedule.campaign_candidate_id == campaign_candidate_id)
            .order_by(InterviewSchedule.round_number.asc())
        )
        return self.db.execute(stmt).scalars().first()

    def get_latest_by_campaign_candidate_id(self, campaign_candidate_id: UUID) -> InterviewSchedule | None:
        stmt = (
            select(InterviewSchedule)
            .where(InterviewSchedule.campaign_candidate_id == campaign_candidate_id)
            .order_by(InterviewSchedule.round_number.desc())
        )
        return self.db.execute(stmt).scalars().first()

    def get_all_by_campaign_candidate_id(self, campaign_candidate_id: UUID) -> list[InterviewSchedule]:
        stmt = (
            select(InterviewSchedule)
            .where(InterviewSchedule.campaign_candidate_id == campaign_candidate_id)
            .order_by(InterviewSchedule.round_number.asc())
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_ended_active_rounds(self, before: datetime) -> list[InterviewSchedule]:
        """
        Epic 5 Step 4 - candidates for the feedback-request sweep: rounds
        whose interview time has passed (end_at < before) but are still
        logistically active (SCHEDULED/RESCHEDULED). Deliberately excludes
        CANCELLED (the interview never happened as planned - nothing to
        give feedback on) and PENDING (end_at is never set). COMPLETED
        also excluded - confirmed dead, nothing writes it since the
        multi-round redesign (a round's completeness is signaled by
        feedback existing, not this status).
        """
        stmt = select(InterviewSchedule).where(
            InterviewSchedule.status.in_([InterviewStatus.SCHEDULED, InterviewStatus.RESCHEDULED]),
            InterviewSchedule.end_at < before,
        )
        return list(self.db.execute(stmt).scalars().all())
    
    def get_started_active_rounds(self, after: datetime) -> list[InterviewSchedule]:
        """
        Epic 5 Step 4 - candidates for the feedback-request sweep: rounds
        whose interview time has started (start_at > after) but are still
        logistically active (SCHEDULED/RESCHEDULED). Deliberately excludes
        CANCELLED (the interview never happened as planned - nothing to
        give feedback on) and PENDING (start_at is never set). COMPLETED
        also excluded - confirmed dead, nothing writes it since the
        multi-round redesign (a round's completeness is signaled by
        feedback existing, not this status).
        """
        stmt = select(InterviewSchedule).where(
            InterviewSchedule.status.in_([InterviewStatus.SCHEDULED, InterviewStatus.RESCHEDULED]),
            InterviewSchedule.start_at > after,
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_by_campaign_id(
        self,
        campaign_id: UUID,
        *,
        start_at_after: datetime | None = None,
        start_at_before: datetime | None = None,
        statuses: list[InterviewStatus] | None = None,
        interviewer_email: str | None = None,
    ) -> list[InterviewSchedule]:
        """
        Campaign-wide interview calendar - the first query in this
        codebase to join interview_schedules to campaign_candidates (every
        other InterviewScheduleRepository method is scoped to one
        candidate/round already). interviewer_email joins to
        interview_interviewers and matches ONLY active rows
        (case-insensitively, matching replace_interviewers()'s own
        lowercase-identity convention) - a round where that person was
        since removed must not show up under their filter.
        """
        stmt = (
            select(InterviewSchedule)
            .join(CampaignCandidate, InterviewSchedule.campaign_candidate_id == CampaignCandidate.id)
            .where(CampaignCandidate.campaign_id == campaign_id)
        )
        if start_at_after is not None:
            stmt = stmt.where(InterviewSchedule.start_at >= start_at_after)
        if start_at_before is not None:
            stmt = stmt.where(InterviewSchedule.start_at <= start_at_before)
        if statuses:
            stmt = stmt.where(InterviewSchedule.status.in_(statuses))
        if interviewer_email:
            stmt = stmt.join(
                InterviewInterviewer, InterviewInterviewer.interview_id == InterviewSchedule.id,
            ).where(
                InterviewInterviewer.is_active.is_(True),
                func.lower(InterviewInterviewer.email) == interviewer_email.lower(),
            )
        stmt = stmt.order_by(InterviewSchedule.start_at.asc())
        return list(self.db.execute(stmt).scalars().all())

    def get_active_interviewers_by_interview_ids(self, interview_ids: list[UUID]) -> list[InterviewInterviewer]:
        """Batch counterpart to get_active_interviewers - one query for a whole campaign's worth of rounds instead of one query per round."""
        if not interview_ids:
            return []
        stmt = select(InterviewInterviewer).where(
            InterviewInterviewer.interview_id.in_(interview_ids),
            InterviewInterviewer.is_active.is_(True),
        )
        return list(self.db.execute(stmt).scalars().all())

    def cancel_active_rounds(
        self, campaign_candidate_id: UUID, reason: str, changed_by: str, changed_by_role: str,
    ) -> list[InterviewSchedule]:
        """
        Cascading-cancellation counterpart to get_or_create_pending, called
        when a candidate's outcome is finalized (leaves INTERVIEW for
        good - see the 3 stage-transition-writing classes' own hooks for
        exactly which target stages trigger this). Any round still in an
        active/logistics-pending status (PENDING/SCHEDULED/RESCHEDULED) is
        cancelled with the given reason, one CANCELLED history entry each
        - same append-only pattern as a manual cancel() call, just
        system-driven. Rounds already CANCELLED are left untouched -
        never re-cancelled, never double-logged.

        changed_by/changed_by_role are NOT NULL on interview_schedule_
        history, so this attributes to whichever real human actor drove
        the pipeline-stage transition that triggered the cascade - never
        a bare SYSTEM/None placeholder. Safe in practice: none of the 3
        target stages that trigger this (SELECTED/REJECTED/SHORTLISTED)
        permit a SYSTEM-only actor in allowed_transitions, so a real
        actor is always available here.

        Returns the rounds actually cancelled (empty list if none were
        active) - callers use this to know whether anything happened,
        not required to react to it.
        """
        cancelled = []
        for round_ in self.get_all_by_campaign_candidate_id(campaign_candidate_id):
            if round_.status not in _ACTIVE_STATUSES:
                continue

            round_.status = InterviewStatus.CANCELLED
            round_.cancel_reason = reason
            round_.updated_at = datetime.now(timezone.utc)
            self.db.flush()

            self.add_history(
                interview_id=round_.id,
                event_type=InterviewHistoryEventType.CANCELLED,
                old_start_at=round_.start_at,
                new_start_at=None,
                changed_by=changed_by,
                changed_by_role=changed_by_role,
                reason=reason,
            )
            cancelled.append(round_)
        return cancelled

    def get_by_id(self, interview_id: UUID) -> InterviewSchedule | None:
        return self.db.get(InterviewSchedule, interview_id)

    def get_or_create_pending(self, campaign_candidate_id: UUID) -> tuple[InterviewSchedule, bool]:
        """
        Epic 4: StageTransitionService.transition()'s INTERVIEW-entry hook -
        check-then-create, not a SAVEPOINT/IntegrityError-catch idempotent
        insert like campaign_candidate_repository.create_idempotent(). That
        pattern exists to survive a genuine race between two independent
        writers; there isn't one here - transition() already holds a
        FOR UPDATE lock on the campaign_candidate row before this ever runs,
        so two calls for the same campaign_candidate_id can never execute
        this concurrently. A plain check-then-create is both sufficient and
        simpler under that guarantee.

        Returns (row, was_created) - was_created=False on a re-entry into
        INTERVIEW (e.g. after a fraud-review clear) that already has a row
        from its first entry; the existing row is returned untouched, never
        reset back to PENDING.
        """
        existing = self.get_by_campaign_candidate_id(campaign_candidate_id)
        if existing is not None:
            return existing, False

        schedule = InterviewSchedule(campaign_candidate_id=campaign_candidate_id)
        self.db.add(schedule)
        self.db.flush()
        self.db.refresh(schedule)
        return schedule, True

    def create_next_round(
        self, campaign_candidate_id: UUID, round_number: int,
    ) -> tuple[InterviewSchedule, bool]:
        """
        Multi-round follow-up: "Schedule Next Round" reservation for
        round_number - SAVEPOINT + IntegrityError-catch (same shape as
        campaign_candidate_repository.create_idempotent()), NOT a plain
        check-then-create like get_or_create_pending above. Unlike that
        method, schedule() holds no FOR UPDATE lock: two concurrent
        "Schedule Next Round" calls for the same candidate (a double-click
        is the realistic case) can both read the same latest round and
        both compute the same next round_number. UNIQUE(
        campaign_candidate_id, round_number) is the real backstop; this
        method is what turns the loser's constraint violation into
        "return the winner's row" instead of a raw IntegrityError.

        begin_nested() scopes the rollback to just this insert attempt -
        it does NOT roll back whatever the caller already flushed earlier
        in the same outer transaction (e.g. marking the previous round
        COMPLETED), only the failed round_number insert itself.

        Returns (row, was_created). was_created=False means a concurrent
        request already created this round_number first - the caller must
        treat the returned row as already fully set up (fields,
        interviewers, calendar event all belong to the winner) and not
        attempt to fill it in again.
        """
        schedule = InterviewSchedule(campaign_candidate_id=campaign_candidate_id, round_number=round_number)
        try:
            with self.db.begin_nested():
                self.db.add(schedule)
                self.db.flush()
        except IntegrityError:
            existing = self.db.execute(
                select(InterviewSchedule).where(
                    InterviewSchedule.campaign_candidate_id == campaign_candidate_id,
                    InterviewSchedule.round_number == round_number,
                )
            ).scalars().first()
            return existing, False

        self.db.refresh(schedule)
        return schedule, True

    def update(self, schedule: InterviewSchedule) -> InterviewSchedule:
        self.db.flush()
        self.db.refresh(schedule)
        return schedule

    def get_interviewers(self, interview_id: UUID) -> list[InterviewInterviewer]:
        """
        Every interview_interviewers row ever created for this round,
        active or since-removed - kept unfiltered specifically for
        get_feedback_for_round (InterviewFeedbackService), which needs to
        resolve a name/email for every interviewer who ever gave feedback,
        including ones since removed from the round. Every other caller
        ("who's on this round right now") should use
        get_active_interviewers() instead.
        """
        stmt = select(InterviewInterviewer).where(InterviewInterviewer.interview_id == interview_id)
        return list(self.db.execute(stmt).scalars().all())

    def get_active_interviewers(self, interview_id: UUID) -> list[InterviewInterviewer]:
        """
        Interviewer lifecycle follow-up - "who's on this round going
        forward": the feedback sweep, the manual Request Feedback button,
        Mark as Completed, the invitation/cancellation-notice hooks, and
        the schedule/reschedule/cancel response all only ever care about
        currently-active interviewers, never ones already soft-removed.
        """
        stmt = select(InterviewInterviewer).where(
            InterviewInterviewer.interview_id == interview_id,
            InterviewInterviewer.is_active.is_(True),
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_interviewer_by_id(self, interviewer_id: UUID) -> InterviewInterviewer | None:
        """
        M12 Step 3 - the feedback token identifies a specific interviewer
        (not just the round, since a round can have several) - callers
        must additionally check the returned row's interview_id matches
        the token's own interview_schedule_id before trusting it; this
        method alone doesn't scope by round.
        """
        return self.db.get(InterviewInterviewer, interviewer_id)

    def replace_interviewers(
        self, interview_id: UUID, interviewers: list[dict],
    ) -> tuple[list[InterviewInterviewer], list[InterviewInterviewer]]:
        """
        Interviewer lifecycle follow-up: diffs against the round's
        currently-ACTIVE interview_interviewers rows by email
        (case-insensitive). Previously this hard-deleted a removed row
        unless it was already referenced by an email_notifications/
        interview_feedback row, in which case the delete was silently
        skipped - two different outcomes depending on reference state,
        neither of which ever let a later get_interviewers() call tell
        "removed" apart from "still on the round." Now unified: a removed
        row is always soft-removed (is_active=false), regardless of
        whether it's referenced - every FK pointing at it stays valid
        either way, so there's no crash risk to route around anymore, and
        exactly one behavior to reason about.

        - Matched by email (against ACTIVE rows only - a removed-then-
          re-added interviewer gets a brand new row, not their old
          inactive one reactivated, so they correctly go through the
          invitation-email path again as a "new" interviewer on the round):
          the existing row's id is kept (so every FK pointing at it stays
          valid) and name is updated in place if it changed.
        - Unmatched incoming entries: inserted fresh, is_active=true.
        - Active existing rows with no match in the incoming list:
          is_active set to false. Never deleted - every FK stays valid,
          and get_interviewers() (unfiltered) still resolves their name/
          email for historical feedback display.

        Returns (active_interviewers, newly_removed_interviewers) - the
        caller queues an invitation email per entry in the first list and
        a removal notice per entry in the second, both deduped at the
        email-queueing layer so a caller can pass the same active list on
        every schedule()/reschedule() call without re-notifying anyone
        already invited for this round.
        """
        existing_rows = self.get_active_interviewers(interview_id)
        existing_by_email = {row.email.lower(): row for row in existing_rows}
        incoming_emails = {i["email"].lower() for i in interviewers}

        result = []
        for i in interviewers:
            existing = existing_by_email.get(i["email"].lower())
            if existing is not None:
                if existing.name != i["name"]:
                    existing.name = i["name"]
                result.append(existing)
            else:
                new_row = InterviewInterviewer(
                    interview_id=interview_id, name=i["name"], email=i["email"], is_active=True,
                )
                self.db.add(new_row)
                result.append(new_row)

        removed = []
        for row in existing_rows:
            if row.email.lower() in incoming_emails:
                continue
            row.is_active = False
            removed.append(row)

        self.db.flush()
        for row in result:
            self.db.refresh(row)
        for row in removed:
            self.db.refresh(row)
        return result, removed

    def add_history(
        self,
        *,
        interview_id: UUID,
        event_type,
        old_start_at,
        new_start_at,
        changed_by: str,
        changed_by_role: str | None,
        reason: str | None,
    ) -> InterviewScheduleHistory:
        entry = InterviewScheduleHistory(
            interview_id=interview_id,
            event_type=event_type,
            old_start_at=old_start_at,
            new_start_at=new_start_at,
            changed_by=changed_by,
            changed_by_role=changed_by_role,
            reason=reason,
        )
        self.db.add(entry)
        self.db.flush()
        self.db.refresh(entry)
        return entry

    def get_history(self, interview_id: UUID) -> list[InterviewScheduleHistory]:
        stmt = (
            select(InterviewScheduleHistory)
            .where(InterviewScheduleHistory.interview_id == interview_id)
            .order_by(InterviewScheduleHistory.changed_at.asc())
        )
        return list(self.db.execute(stmt).scalars().all())

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
