import logging
from datetime import datetime, timezone
from uuid import UUID

from app.enums.constants import ActionType, EntityType
from app.exceptions.campaign_exceptions import CampaignException
from app.models.pipeline import (
    AIEvaluationStatus,
    DecisionSource,
    DecisionType,
    PipelineStage,
    TransitionSource,
)
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.schemas.campaign.override_revert_schema import OverrideRevertResultResponse
from app.services.audit_service import AuditService

logger = logging.getLogger(__name__)


class OverrideRevertService:
    """
    M11-E04-S02-T03 — clear an HR override and restore the automated decision
    it replaced.

    The counterpart to CampaignCandidateService.apply_hr_override, which moves
    REJECTED → SCREENING and stashes the decision it overrode into
    campaign_candidate.decision_details under the `overridden_*` keys. This
    reads those keys back rather than recomputing anything: re-deriving the
    original rejection would mean re-running the deterministic/semantic layers
    against today's JD and weights, which can legitimately produce a different
    verdict than the one actually being reverted.
    """

    def __init__(
        self,
        campaign_candidate_repo: CampaignCandidateRepository,
        audit_service: AuditService,
        ai_evaluation_repo=None,
    ):
        self.campaign_candidate_repo = campaign_candidate_repo
        self.audit_service = audit_service
        self.ai_evaluation_repo = ai_evaluation_repo

    def revert_override(
        self,
        *,
        campaign_candidate_id: UUID,
        reason: str,
        actor_id: str,
        actor_role: str | None,
    ) -> OverrideRevertResultResponse:
        cc = self.campaign_candidate_repo.get_by_id(campaign_candidate_id)
        if cc is None:
            raise CampaignException("Campaign candidate not found.", 404)

        if cc.decision_type != DecisionType.RESET:
            raise CampaignException("This candidate has no active HR override to clear.", 409)

        # An override moves the candidate to SCREENING and re-queues scoring.
        # Once they have moved on from SCREENING that pipeline produced a real
        # outcome, and reverting would silently discard it — so the revert
        # window closes as soon as the override has actually had an effect.
        if cc.pipeline_stage != PipelineStage.SCREENING:
            raise CampaignException(
                "This override can no longer be cleared — the candidate has already progressed to "
                f"{cc.pipeline_stage.value}. Move them manually instead so the change is recorded as a decision.",
                409,
            )

        details = cc.decision_details or {}
        original_type = details.get("overridden_decision_type")
        original_source = details.get("overridden_decision_source")
        if not original_type or not original_source:
            # Pre-dates the unified decision model, or was written by a path
            # that did not stash the original. Refuse rather than invent one.
            raise CampaignException(
                "The decision this override replaced was not recorded, so it cannot be restored "
                "automatically. Reject the candidate manually with a reason instead.",
                409,
            )

        try:
            restored_type = DecisionType(original_type)
            restored_source = DecisionSource(original_source)
        except ValueError:
            raise CampaignException(
                f"Stored decision '{original_type}/{original_source}' is not recognised.", 409,
            )

        override_reason = cc.decision_reason
        now = datetime.now(timezone.utc)

        try:
            # Restore the decision verbatim, then record the clearing itself as
            # its own decision_details entry so the trail reads forwards:
            # rejected → overridden → override cleared.
            cc.pipeline_stage = PipelineStage.REJECTED
            cc.decision_type = restored_type
            cc.decision_source = restored_source
            cc.decision_reason = details.get("overridden_decision_reason")
            cc.decision_at = now
            cc.decision_by_user_id = actor_id
            cc.decision_details = {
                "override_cleared": True,
                "override_cleared_reason": reason,
                "override_cleared_at": now.isoformat(),
                "cleared_override_reason": override_reason,
                "restored_decision_type": restored_type.value,
                "restored_decision_source": restored_source.value,
                "restored_decision_reason": details.get("overridden_decision_reason"),
                "originally_decided_at": details.get("overridden_decision_at"),
            }

            # apply_hr_override forced deterministic_passed=True to let the
            # candidate back through. Only a DETERMINISTIC rejection had it
            # False to begin with — a SEMANTIC rejection had already passed the
            # deterministic layer, so clearing it there would be wrong.
            if restored_source == DecisionSource.DETERMINISTIC:
                cc.deterministic_passed = False

            self.campaign_candidate_repo.update(cc)

            # The override queued a fresh AI evaluation. SKIPPED exists for
            # exactly this shape — queued, then cancelled because the candidate
            # was rejected — and is distinct from PENDING (never queued) and
            # FAILED (ran and errored).
            if self.ai_evaluation_repo is not None:
                try:
                    ai_eval = self.ai_evaluation_repo.get_or_create(cc.id)
                    if ai_eval.ai_evaluation_status in (
                        AIEvaluationStatus.PENDING, AIEvaluationStatus.IN_PROGRESS,
                    ):
                        ai_eval.ai_evaluation_status = AIEvaluationStatus.SKIPPED
                        self.ai_evaluation_repo.update(ai_eval)
                except Exception:
                    # Never fail the revert over the evaluation marker — the
                    # decision itself is the auditable fact.
                    logger.exception(
                        "Could not mark AI evaluation SKIPPED on override revert | cc_id=%s", cc.id,
                    )

            self.campaign_candidate_repo.create_stage_history(
                campaign_candidate_id=cc.id,
                from_stage=PipelineStage.SCREENING,
                to_stage=PipelineStage.REJECTED,
                transition_source=TransitionSource.OVERRIDE,
                changed_by=actor_id,
                change_reason=reason,
            )

            self.audit_service.log(
                actor_id=actor_id,
                actor_role=actor_role,
                action_type=ActionType.CANDIDATE_STAGE_OVERRIDDEN.value,
                entity_type=EntityType.CAMPAIGN_CANDIDATE.value,
                entity_id=cc.id,
                campaign_id=cc.campaign_id,
                details={
                    "title": "HR override cleared — automated decision restored",
                    "override_cleared": True,
                    "clear_reason": reason,
                    "cleared_override_reason": override_reason,
                    "restored_decision_type": restored_type.value,
                    "restored_decision_source": restored_source.value,
                },
            )
            self.campaign_candidate_repo.commit()
        except Exception:
            self.campaign_candidate_repo.rollback()
            raise

        return OverrideRevertResultResponse(
            campaign_candidate_id=cc.id,
            pipeline_stage=cc.pipeline_stage.value,
            restored_decision_type=restored_type.value,
            restored_decision_source=restored_source.value,
            restored_decision_reason=details.get("overridden_decision_reason"),
            cleared_override_reason=override_reason,
            detail="HR override cleared. The original automated decision has been restored.",
        )
