import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from app.enums.constants import ActionType, EntityType
from app.exception_handler.exceptions import NotFoundError
from app.exceptions.pipeline_transition_exceptions import (
    ForbiddenPipelineRoleException,
    InvalidPipelineTransitionException,
    PipelineStageConflictException,
    PipelineTransitionReasonRequiredException,
)
from app.models.pipeline import CampaignCandidate, DecisionSource, DecisionType, PipelineStage, TransitionSource
from app.repositories.allowed_transition_repository import AllowedTransitionRepository
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.interview_schedule_repository import InterviewScheduleRepository
from app.services.audit_service import AuditService
from app.services.campaign.manual_candidate_rescore import enqueue_manual_rescore
from app.services.notifications.candidate_notification_emails import queue_candidate_selected_email

logger = logging.getLogger(__name__)

# M12 cascading-cancellation follow-up: leaving INTERVIEW for one of these
# 3 stages cancels any still-active interview round. The dividing line is
# principled, not arbitrary: SELECTED/REJECTED/SHORTLISTED have no
# return-to-INTERVIEW edge anywhere in allowed_transitions - genuinely
# terminal (or, for SHORTLISTED, a big-enough backward jump past
# HM_REVIEW that any lingering active round would just be stale garbage
# by the time the candidate might reach INTERVIEW again, incorrectly
# satisfying the auto-create-PENDING hook's "already has a row" check).
# FRAUD_REVIEW and HOLD are deliberately excluded - both have a real
# clear/resume edge back to INTERVIEW, so the system already models them
# as reversible pauses, not exits; force-cancelling real scheduled
# logistics over a pause that might resolve in the candidate's favor
# would be a real, avoidable disruption. Same constant, same reasoning,
# duplicated (not shared via import) in PipelineTransitionService and
# CampaignService.override_candidate_stage - the other 2 places a
# candidate can leave INTERVIEW from.
_INTERVIEW_EXIT_CASCADE_STAGES = frozenset(
    {PipelineStage.SELECTED, PipelineStage.REJECTED, PipelineStage.SHORTLISTED, },
)


@dataclass(frozen=True)
class Actor:
    """
    E02: who/what is requesting a pipeline_stage transition. Mirrors
    TokenUser's `roles: list[str]` shape (app/middleware/rbac.py) rather
    than a single role - Epic 1 needs `transition()` to pass for a caller
    holding ANY of an edge's allowed_roles, same any-of-list semantics
    require_roles already uses at the route-gate level, not just the
    caller's first/preferred role.
    """
    roles: list[str]
    id: str | None = None

    @classmethod
    def system(cls) -> "Actor":
        return cls(roles=["SYSTEM"], id=None)


class StageTransitionService:
    """
    M07-E03 S02 T01: validates a pipeline_stage transition against
    allowed_transitions before applying it, and records the change in
    campaign_candidate_stage_history - the single place this rule lives,
    so any future rejection layer (SEMANTIC, AI) reuses the exact same
    validate-then-apply behavior instead of re-implementing it.

    Also the single place that writes the unified decision model
    (decision_type/decision_source/decision_reason/decision_details/
    decision_by_user_id/decision_at) alongside a pipeline_stage move -
    callers pass in what decided the transition, this service applies it
    consistently and packs the same fields into the stage-history
    scores_snapshot so historical decisions remain queryable there too.

    change_reason (the stage-history label, e.g. "Deterministic filter
    rejection") and decision_reason (the specific, candidate-facing reason
    text, e.g. "Missing skill: Python") are deliberately separate
    parameters - callers have always kept these distinct (the 3 scoring
    tasks pass a fixed label as change_reason while building a specific
    rejection_reason string for what used to be CandidateRejection).
    decision_reason defaults to change_reason when the two happen to be
    the same.
    """

    def __init__(
        self,
        allowed_transition_repo: AllowedTransitionRepository,
        campaign_candidate_repo: CampaignCandidateRepository,
        audit_service: AuditService,
        interview_schedule_repo: InterviewScheduleRepository,
    ):
        self.allowed_transition_repo = allowed_transition_repo
        self.campaign_candidate_repo = campaign_candidate_repo
        self.audit_service = audit_service
        # Epic 4: required, not optional-with-a-runtime-check - same
        # reversal this class already went through for audit_service. An
        # optional param whose absence only fails on someone's first real
        # INTERVIEW transition is a trapdoor; every real caller (the DI
        # wiring and all 3 scoring Celery tasks) already has a db session
        # to build one, even the tasks that never reach to_stage=INTERVIEW.
        self.interview_schedule_repo = interview_schedule_repo

    def transition_to_screening(self, campaign_candidate) -> bool:
        """
        Moves campaign_candidate.pipeline_stage from UPLOADED to SCREENING -
        called once, right before deterministic scoring runs, so
        pipeline_stage reflects reality by the time transition_to_rejected's
        own (SCREENING -> REJECTED) check runs for this same candidate.

        A no-op (returns False, pipeline_stage left untouched) whenever the
        candidate isn't currently at UPLOADED - e.g. a retried/redelivered
        scoring task hitting a candidate that already moved on - so this
        never regresses a candidate that has progressed further, and never
        overwrites a HOLD/REJECTED/FRAUD_REVIEW stage.
        """
        from_stage = campaign_candidate.pipeline_stage
        to_stage = PipelineStage.SCREENING

        if from_stage != PipelineStage.UPLOADED:
            return False

        if not self.allowed_transition_repo.is_transition_allowed(from_stage, to_stage):
            logger.error(
                "Stage transition blocked - no allowed_transitions entry | "
                "campaign_candidate_id=%s from_stage=%s to_stage=%s",
                campaign_candidate.id, from_stage.value, to_stage.value,
            )
            return False

        now = datetime.now(timezone.utc)
        campaign_candidate.pipeline_stage = to_stage
        campaign_candidate.updated_at = now
        self.campaign_candidate_repo.update(campaign_candidate)

        self.campaign_candidate_repo.create_stage_history(
            campaign_candidate_id=campaign_candidate.id,
            from_stage=from_stage,
            to_stage=to_stage,
            changed_by=None,
            change_reason="Automated screening started",
            transition_source=TransitionSource.SYSTEM,
            scores_snapshot=None,
        )
        return True

    def transition_on_ai_success(
        self,
        campaign_candidate,
        to_stage: PipelineStage,
        decision_type: DecisionType,
        change_reason: str,
        scores_snapshot: dict | None,
        decision_reason: str | None = None,
        decision_details: dict | None = None,
    ) -> bool:
        """
        AI evaluation's 2 non-REJECT outcomes: a SHORTLIST recommendation
        moves pipeline_stage to SHORTLISTED, a HOLD recommendation moves it
        to HOLD - same validate-then-apply shape as transition_to_rejected,
        always decision_source=AI, always from SCREENING (the only stage
        AI evaluation ever runs from). A no-op (returns False, nothing
        written) if that (SCREENING, to_stage) edge isn't in
        allowed_transitions - never overwrites a candidate that isn't
        actually at SCREENING (e.g. a retried/redelivered evaluation task).

        Trusts the AI's own categorical recommendation exactly the way its
        REJECT outcome already does - not a separate numeric comparison
        against campaign.ai_threshold (deterministic_score/semantic_score
        are already guaranteed above their own thresholds by the time AI
        evaluation runs at all - rejected at an earlier layer otherwise).
        """
        from_stage = campaign_candidate.pipeline_stage

        if not self.allowed_transition_repo.is_transition_allowed(from_stage, to_stage):
            logger.error(
                "Stage transition blocked - no allowed_transitions entry | "
                "campaign_candidate_id=%s from_stage=%s to_stage=%s",
                campaign_candidate.id, from_stage.value, to_stage.value,
            )
            return False

        effective_decision_reason = decision_reason if decision_reason is not None else change_reason

        now = datetime.now(timezone.utc)
        campaign_candidate.pipeline_stage = to_stage
        campaign_candidate.decision_type = decision_type
        campaign_candidate.decision_source = DecisionSource.AI
        campaign_candidate.decision_reason = effective_decision_reason
        campaign_candidate.decision_details = decision_details
        campaign_candidate.decision_by_user_id = None
        campaign_candidate.decision_at = now
        self.campaign_candidate_repo.update(campaign_candidate)

        self.campaign_candidate_repo.create_stage_history(
            campaign_candidate_id=campaign_candidate.id,
            from_stage=from_stage,
            to_stage=to_stage,
            changed_by=None,
            change_reason=change_reason,
            transition_source=TransitionSource.SYSTEM,
            scores_snapshot={
                **(scores_snapshot or {}),
                "decision_type": decision_type.value,
                "decision_source": DecisionSource.AI.value,
                "decision_reason": effective_decision_reason,
                "decision_details": decision_details,
            },
        )
        return True

    def transition_to_rejected(
        self,
        campaign_candidate,
        change_reason: str,
        scores_snapshot: dict | None,
        decision_source: DecisionSource,
        decision_reason: str | None = None,
        decision_details: dict | None = None,
        decision_by_user_id: str | None = None,
    ) -> bool:
        """
        Moves campaign_candidate.pipeline_stage to REJECTED and inserts the
        matching stage-history row, but only if allowed_transitions has a
        (from_stage, REJECTED) entry - otherwise this is a no-op: the
        pipeline_stage is left untouched and no history row is written.
        Returns whether the transition actually happened, so the caller
        (e.g. the rejection email flow) can decide whether it's still
        correct to proceed.

        decision_source identifies who/what made the rejection call
        (DETERMINISTIC/SEMANTIC/AI for the three automated scoring layers,
        or RECRUITER for a future manual rejection) - decision_type is
        always REJECTED here.
        """
        from_stage = campaign_candidate.pipeline_stage
        to_stage = PipelineStage.REJECTED

        if not self.allowed_transition_repo.is_transition_allowed(from_stage, to_stage):
            logger.error(
                "Stage transition blocked - no allowed_transitions entry | "
                "campaign_candidate_id=%s from_stage=%s to_stage=%s",
                campaign_candidate.id, from_stage.value, to_stage.value,
            )
            return False

        effective_decision_reason = decision_reason if decision_reason is not None else change_reason

        now = datetime.now(timezone.utc)
        campaign_candidate.pipeline_stage = to_stage
        campaign_candidate.decision_type = DecisionType.REJECTED
        campaign_candidate.decision_source = decision_source
        campaign_candidate.decision_reason = effective_decision_reason
        campaign_candidate.decision_details = decision_details
        campaign_candidate.decision_by_user_id = decision_by_user_id
        campaign_candidate.decision_at = now
        self.campaign_candidate_repo.update(campaign_candidate)

        self.campaign_candidate_repo.create_stage_history(
            campaign_candidate_id=campaign_candidate.id,
            from_stage=from_stage,
            to_stage=to_stage,
            changed_by=decision_by_user_id,
            change_reason=change_reason,
            transition_source=TransitionSource.SYSTEM if decision_by_user_id is None else TransitionSource.MANUAL,
            scores_snapshot={
                **(scores_snapshot or {}),
                "decision_type": DecisionType.REJECTED.value,
                "decision_source": decision_source.value,
                "decision_reason": effective_decision_reason,
                "decision_details": decision_details,
            },
        )
        return True

    def apply_hr_override(
        self,
        campaign_candidate,
        changed_by: str,
        change_reason: str,
        decision_reason: str | None = None,
    ) -> bool:
        """
        M07-E03 S04 T02: moves campaign_candidate.pipeline_stage from
        REJECTED back to SCREENING for an HR_ADMIN override, and inserts
        the matching stage-history row - same validate-then-apply shape as
        transition_to_rejected, but MANUAL/HR_ADMIN-attributed instead of
        SYSTEM/anonymous. Only if allowed_transitions has a
        (REJECTED, SCREENING) entry; otherwise a no-op (pipeline_stage and
        history are left untouched) and the caller must treat the override
        as failed.

        Captures the decision being overridden (whatever campaign_candidate.
        decision_type/decision_source/decision_reason currently hold, i.e.
        the rejection this override reverses) into decision_details before
        overwriting them with the RESET decision - the caller can read
        campaign_candidate.decision_details back after this call succeeds
        for its own audit-log/reporting needs, rather than needing to
        snapshot the prior state itself.

        decision_reason is the HR admin's actual override justification
        text (what used to be stored as hr_override_reason) - distinct
        from change_reason, which is a fixed stage-history label; defaults
        to change_reason when the two happen to be the same.
        """
        from_stage = campaign_candidate.pipeline_stage
        to_stage = PipelineStage.SCREENING

        if not self.allowed_transition_repo.is_transition_allowed(from_stage, to_stage):
            logger.error(
                "HR override blocked - no allowed_transitions entry | "
                "campaign_candidate_id=%s from_stage=%s to_stage=%s",
                campaign_candidate.id, from_stage.value, to_stage.value,
            )
            return False

        effective_decision_reason = decision_reason if decision_reason is not None else change_reason

        now = datetime.now(timezone.utc)
        decision_details = {
            "overridden_decision_type": (
                campaign_candidate.decision_type.value if campaign_candidate.decision_type else None
            ),
            "overridden_decision_source": (
                campaign_candidate.decision_source.value if campaign_candidate.decision_source else None
            ),
            "overridden_decision_reason": campaign_candidate.decision_reason,
            "overridden_decision_at": (
                campaign_candidate.decision_at.isoformat() if campaign_candidate.decision_at else None
            ),
        }

        campaign_candidate.pipeline_stage = to_stage
        campaign_candidate.decision_type = DecisionType.RESET
        campaign_candidate.decision_source = DecisionSource.HR_ADMIN
        campaign_candidate.decision_reason = effective_decision_reason
        campaign_candidate.decision_details = decision_details
        campaign_candidate.decision_by_user_id = changed_by
        campaign_candidate.decision_at = now
        self.campaign_candidate_repo.update(campaign_candidate)

        self.campaign_candidate_repo.create_stage_history(
            campaign_candidate_id=campaign_candidate.id,
            from_stage=from_stage,
            to_stage=to_stage,
            changed_by=changed_by,
            change_reason=change_reason,
            transition_source=TransitionSource.MANUAL,
            scores_snapshot={
                "decision_type": DecisionType.RESET.value,
                "decision_source": DecisionSource.HR_ADMIN.value,
                "decision_reason": effective_decision_reason,
                "decision_details": decision_details,
            },
        )
        return True

    def transition(
        self,
        campaign_candidate_id: UUID,
        to_stage: PipelineStage,
        actor: Actor,
        reason: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[CampaignCandidate, bool]:
        """
        E02: single validated entry point for a pipeline_stage move. Checked
        in this exact order - existence, then role, then reason, then a
        concurrency re-check, then an idempotent write - so a caller always
        gets back the error that actually matches what went wrong (an
        invalid transition is never reported as a role problem just
        because role happened to be checked first).

        Does NOT commit until the very end, after the audit_log write
        succeeds - same convention as every other method in this class
        (apply_hr_override/transition_to_rejected above), except here it
        matters for more than style: nothing before the final commit() is
        persisted, so a failed audit write leaves the stage-history insert
        and the pipeline_stage change uncommitted too, not half-applied.

        Separate from transition_to_rejected/apply_hr_override - neither of
        those two is touched or refactored to call this.
        """
        candidate = self.campaign_candidate_repo.get_by_id(campaign_candidate_id)
        if candidate is None:
            raise NotFoundError("Campaign candidate not found.")

        from_stage = candidate.pipeline_stage

        # 1. Existence check. AllowedTransitionRepository.get() (not
        # is_transition_allowed()) - the role/reason checks below need
        # allowed_roles/requires_reason off this same row.
        transition_row = self.allowed_transition_repo.get(from_stage, to_stage)
        if transition_row is None:
            raise InvalidPipelineTransitionException(from_stage.value, to_stage.value)

        # 2. Actor/role check. A single membership test covers both cases
        # the ticket describes separately (SYSTEM must be listed; a human
        # role must be listed) - they're the same check, since "SYSTEM" is
        # just another string in allowed_roles, not a special case at the
        # DB level. Deliberately an `if`/raise, not a bare `assert` - `python
        # -O` strips asserts, which would silently turn this into no check
        # at all in an optimized build.
        if not any(r in transition_row.allowed_roles for r in actor.roles):
            raise ForbiddenPipelineRoleException(from_stage.value, to_stage.value, actor.roles)

        # 3. Reason check.
        if transition_row.requires_reason and not (reason and reason.strip()):
            raise PipelineTransitionReasonRequiredException(from_stage.value, to_stage.value)

        # 4. State check. The FOR UPDATE lock is acquired first, and the
        # pipeline_stage comparison reads off that locked row - not the
        # unlocked `candidate` fetched above - otherwise this would just be
        # comparing two stale reads and would never actually catch a race.
        locked_candidate = self.campaign_candidate_repo.get_by_id_for_update(campaign_candidate_id)
        if locked_candidate.pipeline_stage != from_stage:
            raise PipelineStageConflictException(from_stage.value)

        is_system = "SYSTEM" in actor.roles
        changed_by = None if is_system else actor.id
        transition_source = TransitionSource.SYSTEM if is_system else TransitionSource.MANUAL

        # 5. Idempotent write - same SAVEPOINT + IntegrityError-catch shape
        # as campaign_candidate_repository.create_idempotent().
        history, was_created = self.campaign_candidate_repo.create_stage_history_idempotent(
            campaign_candidate_id=campaign_candidate_id,
            from_stage=from_stage,
            to_stage=to_stage,
            changed_by=changed_by,
            change_reason=reason,
            transition_source=transition_source,
            idempotency_key=idempotency_key,
        )

        if not was_created:
            # A retried request under the same idempotency key - return the
            # existing history/candidate state. Do not re-apply the stage
            # move, do not write a second audit entry.
            self.campaign_candidate_repo.commit()
            return locked_candidate, False

        # 6. Apply the stage move + audit log, same uncommitted transaction
        # as the history insert above.
        locked_candidate.pipeline_stage = to_stage
        self.campaign_candidate_repo.update(locked_candidate)

        # Epic 4: INTERVIEW-entry hook - same transaction as the stage move
        # above, not a best-effort follow-up after commit (unlike
        # apply_hr_override's post-commit re-evaluation queueing, which is
        # forced post-commit because it enqueues a Celery task - a
        # cross-system call that can't be rolled back with the DB
        # transaction). Creating an interview_schedules row is a plain
        # INSERT on this same session; campaign_candidate_id's UNIQUE
        # constraint is a hard invariant ("every candidate at INTERVIEW has
        # exactly one row"), not a convention, so a failure here must roll
        # back the stage move too, not leave a candidate at INTERVIEW with
        # no row for the schedule endpoint to act on.
        if to_stage == PipelineStage.INTERVIEW:
            # get_or_create, not a blind insert: a candidate re-entering
            # INTERVIEW (e.g. after a fraud-review clear) already has a row
            # from its first entry, and that row must be left untouched -
            # never reset back to PENDING.
            self.interview_schedule_repo.get_or_create_pending(locked_candidate.id)

        # Logs the role that actually permitted this transition, not an
        # arbitrary one - matters once actor.roles can hold roles the edge
        # doesn't list at all (e.g. a HIRING_MANAGER+HR_ADMIN actor on an
        # edge only HIRING_MANAGER can use).
        resolved_role = next(r for r in actor.roles if r in transition_row.allowed_roles)

        # M12 cascading-cancellation hook - opposite direction of the
        # INTERVIEW-entry hook above, same transaction, same "a failure
        # here rolls back the whole transition" reasoning. changed_by is
        # never None for these 3 target stages (see
        # _INTERVIEW_EXIT_CASCADE_STAGES's own comment - none of them
        # permit a SYSTEM-only actor in allowed_transitions), so this
        # always has a real actor to attribute the cancellation to.
        if from_stage == PipelineStage.INTERVIEW and to_stage in _INTERVIEW_EXIT_CASCADE_STAGES:
            self.interview_schedule_repo.cancel_active_rounds(
                locked_candidate.id,
                reason=f"Candidate outcome finalized: {to_stage.value}",
                changed_by=changed_by,
                changed_by_role=resolved_role,
            )

        # Epic 5 follow-up - manual re-score trigger: arriving at
        # SCREENING from anywhere other than UPLOADED (the automated
        # resume-upload path, which scores the candidate itself and must
        # never get this hook - see manual_candidate_rescore.py's own
        # docstring for why) cancels any still-active interview rounds.
        # Same transaction, same reasoning as the cascade-cancel hook
        # above - matters in practice for a candidate currently paused at
        # HOLD/FRAUD_REVIEW (the only 2 stages that can both reach
        # SCREENING and still have active rounds); a harmless no-op
        # otherwise.
        if to_stage == PipelineStage.SCREENING and from_stage != PipelineStage.UPLOADED:
            self.interview_schedule_repo.cancel_active_rounds(
                locked_candidate.id,
                reason="Candidate returned to SCREENING for re-evaluation",
                changed_by=changed_by,
                changed_by_role=resolved_role,
            )

        self.audit_service.log(
            actor_id=changed_by,
            actor_role=resolved_role,
            action_type=ActionType.PIPELINE_STAGE_TRANSITIONED,
            entity_type=EntityType.CAMPAIGN_CANDIDATE,
            entity_id=locked_candidate.id,
            campaign_id=locked_candidate.campaign_id,
            details={
                "from_stage": from_stage.value,
                "to_stage": to_stage.value,
                "reason": reason,
                "stage_history_id": str(history.id),
            },
        )

        self.campaign_candidate_repo.commit()

        # Epic 5 Step 2 - best-effort, after commit, same reasoning as
        # _queue_rejection_email: a failure to queue/send this must never
        # undo the already-committed transition.
        if to_stage == PipelineStage.SELECTED:
            queue_candidate_selected_email(self.campaign_candidate_repo.db, locked_candidate)

        # Epic 5 follow-up - manual re-score trigger, post-commit (unlike
        # the cascade-cancel above, this enqueues a Celery task - a
        # cross-system call that can't be rolled back with the DB
        # transaction, same reasoning as queue_candidate_selected_email
        # above and _queue_post_override_evaluation elsewhere). Never
        # fires for the automated UPLOADED->SCREENING path - see
        # manual_candidate_rescore.py.
        if to_stage == PipelineStage.SCREENING and from_stage != PipelineStage.UPLOADED:
            enqueue_manual_rescore(self.campaign_candidate_repo.db, locked_candidate)

        return locked_candidate, True
