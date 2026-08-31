from datetime import datetime, timezone

from app.enums.constants import ActionType, EntityType
from app.exceptions.pipeline_transition_exceptions import (
    ForbiddenPipelineRoleException,
    InvalidPipelineTransitionException,
    PipelineTransitionReasonRequiredException,
)
from app.models.campaigns import CampaignStatus
from app.models.pipeline import (
    CampaignCandidate,
    DecisionSource,
    DecisionType,
    PipelineStage,
    TransitionSource,
)
from app.repositories.allowed_transition_repository import AllowedTransitionRepository
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.interview_schedule_repository import InterviewScheduleRepository
from app.services.audit_service import AuditService

# M12 cascading-cancellation follow-up: same constant, same reasoning as
# StageTransitionService's own copy (see that file for the full
# rationale) - duplicated, not imported, since this is the second of the
# 3 places a candidate can leave INTERVIEW from (this one backs Pipeline
# Board drag-and-drop, where INTERVIEW -> SHORTLISTED is only reachable
# from in the first place - Epic 1's dedicated endpoints never expose it).
_INTERVIEW_EXIT_CASCADE_STAGES = frozenset(
    {PipelineStage.SELECTED, PipelineStage.REJECTED, PipelineStage.SHORTLISTED},
)


class PipelineTransitionService:
    """
    Epic 3 (M05-E03) Phase C0 — the one generic, validated way to move a
    campaign_candidate between pipeline stages, backing both
    move_pipeline_stage (Pipeline Board drag-and-drop) and
    BulkStageMoveService. NOT a dead/unused class - the class docstring
    used to claim "zero call sites anywhere in the codebase", which was
    true when Epic 3 wrote it but went stale once those two real callers
    landed; corrected here since that false claim is exactly what let the
    Epic 4 INTERVIEW-entry hook below go missing from this class for as
    long as it did (nobody checked back in on "still zero callers?").

    Decision-model-aware for consistency with StageTransitionService (the
    service actually wired into the 3 scoring Celery tasks today) - when a
    decision_type is supplied, the same decision_*/scores_snapshot fields
    get written, so a caller of this generic engine (e.g. a recruiter
    shortlist/reject action) doesn't need its own copy of that logic.
    """

    def __init__(
        self,
        allowed_transition_repo: AllowedTransitionRepository,
        campaign_candidate_repo: CampaignCandidateRepository,
        audit_service: AuditService,
        interview_schedule_repo: InterviewScheduleRepository,
        campaign_repo: CampaignRepository | None = None,
    ):
        self.allowed_transition_repo = allowed_transition_repo
        self.campaign_candidate_repo = campaign_candidate_repo
        self.audit_service = audit_service
        # Required, not optional-with-a-runtime-check - same discipline
        # already applied to StageTransitionService.interview_schedule_repo
        # after this exact class of gap was found live (a candidate moved
        # to INTERVIEW via Pipeline Board drag-and-drop with no
        # interview_schedules row at all, discovered mid frontend
        # integration testing). This is the second of two generic
        # transition engines that can reach to_stage=INTERVIEW - both must
        # carry the same hook, not just the one Epic 4 originally touched.
        self.interview_schedule_repo = interview_schedule_repo
        # Optional so pre-existing construction sites keep working; when it is
        # absent the openings cap simply isn't enforced by this service.
        self.campaign_repo = campaign_repo

    def transition_stage(
        self,
        campaign_candidate: CampaignCandidate,
        to_stage: PipelineStage,
        changed_by: str | None = None,
        actor_role: str | None = None,
        reason: str | None = None,
        source: TransitionSource = TransitionSource.SYSTEM,
        decision_type: DecisionType | None = None,
        decision_source: DecisionSource | None = None,
        decision_details: dict | None = None,
    ) -> CampaignCandidate:
        """
        Does NOT commit — the caller commits, same convention as every
        other service in this codebase. changed_by should be supplied
        whenever a real actor (human or a specific automated trigger) is
        driving the transition: it's stored on the history row regardless,
        but the PIPELINE_STAGE_TRANSITIONED audit event is only written
        when it's present — an unattributed SYSTEM transition still gets
        its history row, just not an audit entry.

        Epic 3 Fix 2 note: the "since AuditLog.actor_id is a required FK"
        reasoning this comment used to give for that choice was false -
        actor_id is nullable (both model and live schema), and
        StageTransitionService.transition() already logs SYSTEM-triggered
        writes correctly with actor_id=None/actor_role=SYSTEM. Not changed
        here to always log with actor_id=None instead of skipping, since
        this method has zero real call sites anywhere in the codebase
        (see the class docstring) - that's a behavior change with nothing
        to verify it against, not a mis-attribution fix like the 4 real
        call sites fixed elsewhere in Epic 3 Fix 2.

        decision_type/decision_source/decision_details are optional -
        when supplied, the unified decision model is written onto
        campaign_candidate (decision_reason/decision_by_user_id/decision_at
        derive from reason/changed_by/now) and folded into the
        stage-history scores_snapshot alongside the transition itself.
        """
        from_stage = campaign_candidate.pipeline_stage

        transition = self.allowed_transition_repo.get(from_stage, to_stage)
        if transition is None:
            raise InvalidPipelineTransitionException(from_stage.value, to_stage.value)

        effective_role = actor_role or source.value
        if effective_role not in transition.allowed_roles:
            raise ForbiddenPipelineRoleException(from_stage.value, to_stage.value, [effective_role])

        if transition.requires_reason and not reason:
            raise PipelineTransitionReasonRequiredException(from_stage.value, to_stage.value)

        self.campaign_candidate_repo.update_pipeline_stage(campaign_candidate, to_stage)

        if to_stage == PipelineStage.INTERVIEW:
            # Same-transaction, same reasoning as
            # StageTransitionService.transition()'s own hook: creating the
            # PENDING interview_schedules row is a plain INSERT on this
            # same session, and campaign_candidate_id's UNIQUE constraint
            # is a hard invariant ("every candidate at INTERVIEW has
            # exactly one row"), not a convention - a failure here must
            # roll back the stage move too. get_or_create, not a blind
            # insert, for the same re-entry reason (e.g. a fraud-review
            # clear routed through this engine instead of the other one).
            self.interview_schedule_repo.get_or_create_pending(campaign_candidate.id)

        # M12 cascading-cancellation hook - opposite direction of the
        # INTERVIEW-entry hook above, same transaction. Both of this
        # class's real callers (move_pipeline_stage/BulkStageMoveService)
        # always pass a real changed_by/actor_role - see
        # _INTERVIEW_EXIT_CASCADE_STAGES's own comment for why none of
        # these 3 target stages ever permit a SYSTEM-only actor anyway.
        if from_stage == PipelineStage.INTERVIEW and to_stage in _INTERVIEW_EXIT_CASCADE_STAGES:
            self.interview_schedule_repo.cancel_active_rounds(
                campaign_candidate.id,
                reason=f"Candidate outcome finalized: {to_stage.value}",
                changed_by=changed_by,
                changed_by_role=effective_role,
            )

        # Epic 5 follow-up - manual re-score trigger: arriving at
        # SCREENING from anywhere other than UPLOADED cancels any still-
        # active interview rounds, same transaction, same reasoning as
        # the cascade-cancel hook above. The re-score itself is NOT
        # enqueued here - this method never commits, and enqueueing a
        # Celery task can't be rolled back with the DB transaction, so
        # that part lives in this method's 3 real callers instead
        # (move_pipeline_stage, BulkStageMoveService.bulk_move/move_one),
        # post their own commit - see manual_candidate_rescore.py.
        if to_stage == PipelineStage.SCREENING and from_stage != PipelineStage.UPLOADED:
            self.interview_schedule_repo.cancel_active_rounds(
                campaign_candidate.id,
                reason="Candidate returned to SCREENING for re-evaluation",
                changed_by=changed_by,
                changed_by_role=effective_role,
            )

        scores_snapshot = None
        if decision_type is not None:
            now = datetime.now(timezone.utc)
            campaign_candidate.decision_type = decision_type
            campaign_candidate.decision_source = decision_source
            campaign_candidate.decision_reason = reason
            campaign_candidate.decision_details = decision_details
            campaign_candidate.decision_by_user_id = changed_by
            campaign_candidate.decision_at = now
            self.campaign_candidate_repo.update(campaign_candidate)
            scores_snapshot = {
                "decision_type": decision_type.value,
                "decision_source": decision_source.value if decision_source else None,
                "decision_reason": reason,
                "decision_details": decision_details,
            }

        if to_stage == PipelineStage.SELECTED:
            self._close_if_all_positions_filled(
                campaign_candidate, changed_by=changed_by, actor_role=actor_role,
            )

        self.campaign_candidate_repo.create_stage_history(
            campaign_candidate_id=campaign_candidate.id,
            to_stage=to_stage,
            from_stage=from_stage,
            changed_by=changed_by,
            change_reason=reason,
            transition_source=source,
            scores_snapshot=scores_snapshot,
        )

        if changed_by is not None:
            self.audit_service.log(
                actor_id=changed_by,
                actor_role=actor_role,
                action_type=ActionType.PIPELINE_STAGE_TRANSITIONED,
                entity_type=EntityType.CAMPAIGN_CANDIDATE,
                entity_id=campaign_candidate.id,
                details={
                    "from_stage": from_stage.value,
                    "to_stage": to_stage.value,
                    "reason": reason,
                },
            )

        return campaign_candidate

    def _close_if_all_positions_filled(
        self,
        campaign_candidate: CampaignCandidate,
        changed_by: str | None = None,
        actor_role: str | None = None,
    ) -> bool:
        """
        max_candidates is the number of openings, so a slot is consumed when a
        candidate reaches SELECTED — not when a resume is uploaded. Once every
        opening is filled the campaign auto-closes, which is where the old
        intake-time CAP_REACHED auto-close used to live.

        The campaign row is locked (SELECT ... FOR UPDATE) before counting so
        two concurrent selections cannot both read "one slot left" and
        overshoot the opening count. Does not commit — the caller does.
        """
        if self.campaign_repo is None:
            return False

        campaign = self.campaign_repo.get_by_id_for_update(campaign_candidate.campaign_id)
        if campaign is None or not campaign.max_candidates:
            return False
        if campaign.status == CampaignStatus.CLOSED:
            return False

        # update_pipeline_stage has already flushed this candidate's new stage,
        # so the count below includes the selection that triggered this call.
        if self.campaign_repo.get_selected_count(campaign.id) < campaign.max_candidates:
            return False

        campaign.status = CampaignStatus.CLOSED
        campaign.updated_at = datetime.now(timezone.utc)
        self.campaign_repo.update(campaign)

        if changed_by is not None:
            self.audit_service.log(
                actor_id=changed_by,
                actor_role=actor_role,
                action_type=ActionType.CAMPAIGN_AUTO_CLOSED,
                entity_type=EntityType.CAMPAIGN,
                entity_id=campaign.id,
                campaign_id=campaign.id,
                details={
                    "title": f"Campaign '{campaign.name}' auto-closed",
                    "reason": "ALL_POSITIONS_FILLED",
                    "max_candidates": campaign.max_candidates,
                    "selected_count": campaign.max_candidates,
                },
            )

        return True
