import logging

from app.models.pipeline import PipelineStage, TransitionSource
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
    ) -> bool:
        """
        Moves campaign_candidate.pipeline_stage to REJECTED and inserts the
        matching stage-history row, but only if allowed_transitions has a
        (from_stage, REJECTED) entry - otherwise this is a no-op: the
        pipeline_stage is left untouched and no history row is written.
        Returns whether the transition actually happened, so the caller
        (e.g. the rejection email flow) can decide whether it's still
        correct to proceed.
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

        campaign_candidate.pipeline_stage = to_stage
        self.campaign_candidate_repo.update(campaign_candidate)

        self.campaign_candidate_repo.create_stage_history(
            campaign_candidate_id=campaign_candidate.id,
            from_stage=from_stage,
            to_stage=to_stage,
            changed_by=None,
            change_reason=change_reason,
            transition_source=TransitionSource.SYSTEM,
            scores_snapshot=scores_snapshot,
        )
        return True

    def apply_hr_override(
        self,
        campaign_candidate,
        changed_by: str,
        change_reason: str,
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

        campaign_candidate.pipeline_stage = to_stage
        self.campaign_candidate_repo.update(campaign_candidate)

        self.campaign_candidate_repo.create_stage_history(
            campaign_candidate_id=campaign_candidate.id,
            from_stage=from_stage,
            to_stage=to_stage,
            changed_by=changed_by,
            change_reason=change_reason,
            transition_source=TransitionSource.MANUAL,
            scores_snapshot=None,
        )
        return True
