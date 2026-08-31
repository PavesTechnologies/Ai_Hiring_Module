from uuid import UUID

from app.enums.constants import ActionType, EntityType
from app.exceptions.campaign_exceptions import CampaignException
from app.models.pipeline import DecisionSource, DecisionType, PipelineStage, TransitionSource
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.schemas.campaign.bulk_stage_move_schema import (
    BulkStageMoveResultResponse,
    SingleStageMoveResultResponse,
)
from app.services.audit_service import AuditService
from app.services.campaign.manual_candidate_rescore import enqueue_manual_rescore
from app.services.campaign.pipeline_transition_service import PipelineTransitionService

# The unified decision model replaced hr_override; a manual stage move records
# the decision it represents so the reason survives on the candidate itself.
_STAGE_TO_DECISION = {
    PipelineStage.SHORTLISTED: DecisionType.SHORTLISTED,
    PipelineStage.SELECTED: DecisionType.SELECTED,
    PipelineStage.REJECTED: DecisionType.REJECTED,
    PipelineStage.HOLD: DecisionType.HOLD,
    PipelineStage.FRAUD_REVIEW: DecisionType.FRAUD_REVIEW,
}


class BulkStageMoveService:
    """
    M11-E04-S03 — manual pipeline stage moves.

    T01 moves one candidate, T02 moves a batch, T03 rejects one. All three go
    through PipelineTransitionService, so allowed_transitions, the mandatory
    reason, stage history and the openings cap apply identically no matter
    which entry point is used — the only difference is what gets audited.
    """

    def __init__(
        self,
        campaign_candidate_repo: CampaignCandidateRepository,
        pipeline_transition_service: PipelineTransitionService,
        audit_service: AuditService,
    ):
        self.campaign_candidate_repo = campaign_candidate_repo
        self.pipeline_transition_service = pipeline_transition_service
        self.audit_service = audit_service

    def bulk_move(
        self,
        *,
        campaign_id: UUID,
        campaign_candidate_ids: list[UUID],
        target_stage: str,
        reason: str,
        actor_id: str,
        actor_role: str | None,
    ) -> BulkStageMoveResultResponse:
        try:
            to_stage = PipelineStage(target_stage)
        except ValueError:
            raise CampaignException(f"Unknown pipeline stage '{target_stage}'.", 422)

        candidates = []
        skipped: list[dict] = []
        for cc_id in campaign_candidate_ids:
            cc = self.campaign_candidate_repo.get_by_id(cc_id)
            if cc is None or cc.campaign_id != campaign_id:
                # never silently drop an id the caller asked about
                skipped.append({"campaign_candidate_id": str(cc_id), "reason": "Not found in this campaign."})
                continue
            candidates.append(cc)

        if not candidates:
            raise CampaignException("None of the selected candidates belong to this campaign.", 404)

        # Spec: a batch move requires a single shared source stage. Mixed
        # selections are rejected outright rather than partially applied,
        # because "next stage" means different things from different stages.
        source_stages = {c.pipeline_stage for c in candidates}
        if len(source_stages) > 1:
            raise CampaignException(
                "All selected candidates must currently be in the same stage. "
                f"Selection spans: {', '.join(sorted(s.value for s in source_stages))}.",
                409,
            )

        from_stage = next(iter(source_stages))
        if from_stage == to_stage:
            raise CampaignException("Candidates are already in that stage.", 409)

        decision_type = _STAGE_TO_DECISION.get(to_stage)

        try:
            for cc in candidates:
                # Each move goes through the one validated transition engine, so
                # allowed_transitions, the reason requirement, stage history and
                # the openings cap all apply exactly as they do to single moves.
                self.pipeline_transition_service.transition_stage(
                    cc,
                    to_stage,
                    changed_by=actor_id,
                    actor_role=actor_role,
                    reason=reason,
                    source=TransitionSource.MANUAL,
                    decision_type=decision_type,
                    decision_source=DecisionSource.HR_ADMIN,
                )

            # One audit row for the batch, not one per candidate — the spec
            # wants the batch itself to be the auditable event.
            self.audit_service.log(
                actor_id=actor_id,
                actor_role=actor_role,
                action_type=ActionType.CANDIDATE_STAGE_OVERRIDDEN.value,
                entity_type=EntityType.CAMPAIGN.value,
                entity_id=campaign_id,
                campaign_id=campaign_id,
                details={
                    "title": f"{len(candidates)} candidate(s) moved {from_stage.value} → {to_stage.value}",
                    "bulk": True,
                    "moved_count": len(candidates),
                    "from_stage": from_stage.value,
                    "to_stage": to_stage.value,
                    "reason": reason,
                    "skipped_count": len(skipped),
                },
            )
            self.campaign_candidate_repo.commit()
            # Selection email is no longer sent automatically here - see
            # CampaignCandidateService.send_selection_email (manual send
            # button, matching the "Send Rejection Email" precedent).
            # Epic 5 follow-up - manual re-score trigger, post-commit
            # (see manual_candidate_rescore.py). Never fires for the
            # automated UPLOADED->SCREENING path.
            if to_stage == PipelineStage.SCREENING and from_stage != PipelineStage.UPLOADED:
                for cc in candidates:
                    enqueue_manual_rescore(self.campaign_candidate_repo.db, cc)
        except Exception:
            self.campaign_candidate_repo.rollback()
            raise

        return BulkStageMoveResultResponse(
            moved_count=len(candidates),
            from_stage=from_stage.value,
            to_stage=to_stage.value,
            skipped=skipped,
            detail=f"Moved {len(candidates)} candidate(s) from {from_stage.value} to {to_stage.value}.",
        )

    # ── T01 / T03 — single candidate ──────────────────────────────────

    def _load_in_campaign(self, campaign_id: UUID, campaign_candidate_id: UUID):
        cc = self.campaign_candidate_repo.get_by_id(campaign_candidate_id)
        # Checked together: a candidate that exists but belongs to another
        # campaign must not be movable through this campaign's URL.
        if cc is None or cc.campaign_id != campaign_id:
            raise CampaignException("Candidate not found in this campaign.", 404)
        return cc

    def move_one(
        self,
        *,
        campaign_id: UUID,
        campaign_candidate_id: UUID,
        target_stage: str,
        reason: str,
        actor_id: str,
        actor_role: str | None,
    ) -> SingleStageMoveResultResponse:
        """
        M11-E04-S03-T01. Deliberately not implemented as bulk_move([one]) —
        a single move audits the candidate as the entity, which is what the
        candidate's own activity timeline reads, whereas a batch audits the
        campaign.
        """
        try:
            to_stage = PipelineStage(target_stage)
        except ValueError:
            raise CampaignException(f"Unknown pipeline stage '{target_stage}'.", 422)

        cc = self._load_in_campaign(campaign_id, campaign_candidate_id)
        from_stage = cc.pipeline_stage
        if from_stage == to_stage:
            raise CampaignException("Candidate is already in that stage.", 409)

        try:
            self.pipeline_transition_service.transition_stage(
                cc,
                to_stage,
                changed_by=actor_id,
                actor_role=actor_role,
                reason=reason,
                source=TransitionSource.MANUAL,
                decision_type=_STAGE_TO_DECISION.get(to_stage),
                decision_source=DecisionSource.HR_ADMIN,
            )
            self.audit_service.log(
                actor_id=actor_id,
                actor_role=actor_role,
                action_type=ActionType.CANDIDATE_STAGE_OVERRIDDEN.value,
                entity_type=EntityType.CAMPAIGN_CANDIDATE.value,
                entity_id=campaign_candidate_id,
                campaign_id=campaign_id,
                details={
                    "title": f"Candidate moved {from_stage.value} → {to_stage.value}",
                    "bulk": False,
                    "from_stage": from_stage.value,
                    "to_stage": to_stage.value,
                    "reason": reason,
                },
            )
            self.campaign_candidate_repo.commit()
            # Selection email is no longer sent automatically here - see
            # CampaignCandidateService.send_selection_email (manual send
            # button, matching the "Send Rejection Email" precedent).
            # Epic 5 follow-up - manual re-score trigger, post-commit
            # (see manual_candidate_rescore.py). Never fires for the
            # automated UPLOADED->SCREENING path.
            if to_stage == PipelineStage.SCREENING and from_stage != PipelineStage.UPLOADED:
                enqueue_manual_rescore(self.campaign_candidate_repo.db, cc)
        except Exception:
            self.campaign_candidate_repo.rollback()
            raise

        return SingleStageMoveResultResponse(
            campaign_candidate_id=campaign_candidate_id,
            from_stage=from_stage.value,
            to_stage=to_stage.value,
            detail=f"Moved candidate from {from_stage.value} to {to_stage.value}.",
        )

    def reject_one(
        self,
        *,
        campaign_id: UUID,
        campaign_candidate_id: UUID,
        reason: str,
        actor_id: str,
        actor_role: str | None,
    ) -> SingleStageMoveResultResponse:
        """
        M11-E04-S03-T03 — manual rejection from the candidate list.

        A thin, explicit wrapper over move_one rather than a second code path:
        rejection IS a stage move, and routing it anywhere else is how the
        decision record and the openings cap drift apart.
        """
        cc = self._load_in_campaign(campaign_id, campaign_candidate_id)
        if cc.pipeline_stage == PipelineStage.REJECTED:
            raise CampaignException("Candidate is already rejected.", 409)

        return self.move_one(
            campaign_id=campaign_id,
            campaign_candidate_id=campaign_candidate_id,
            target_stage=PipelineStage.REJECTED.value,
            reason=reason,
            actor_id=actor_id,
            actor_role=actor_role,
        )
