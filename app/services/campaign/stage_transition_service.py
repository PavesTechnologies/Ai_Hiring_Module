import logging
from datetime import datetime, timezone

from app.models.pipeline import DecisionSource, DecisionType, PipelineStage, TransitionSource
from app.repositories.allowed_transition_repository import AllowedTransitionRepository
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository

logger = logging.getLogger(__name__)


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
    ):
        self.allowed_transition_repo = allowed_transition_repo
        self.campaign_candidate_repo = campaign_candidate_repo

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
