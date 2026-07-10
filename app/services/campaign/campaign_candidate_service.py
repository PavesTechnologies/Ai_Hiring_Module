from uuid import UUID
import uuid

from app.dependencies import campaign
from app.dependencies import campaign
from app.enums.constants import ActionType, EntityType
from app.enums.constants import EntityType
from app.exceptions.campaign_exceptions import CampaignException
from app.models.campaigns import CampaignStatus
from app.models.pipeline import (
    CampaignCandidate,
    PipelineStage,
)
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.campaign_candidate_repository import (
    CampaignCandidateRepository,
)
from app.schemas.campaign.campaign_candidate_schema import (
    CampaignCandidateCreateRequest,
    CampaignCandidateResponse,
)
from app.services.audit_service import AuditService


class CampaignCandidateService:

    def __init__(
        self,
        campaign_repo: CampaignRepository,
        campaign_candidate_repo: CampaignCandidateRepository,
        audit_service: AuditService,
    ):
        self.campaign_repo = campaign_repo
        self.campaign_candidate_repo = campaign_candidate_repo
        self.audit_service = audit_service

    def create_campaign_candidate(
        self,
        request: CampaignCandidateCreateRequest,
    ) -> CampaignCandidateResponse:

        try:

            # -----------------------------
            # Validate Campaign
            # -----------------------------
            campaign = self.campaign_repo.get_by_id(
                request.campaign_id
            )

            if not campaign:
                raise CampaignException(
                    "Campaign not found.",
                    404,
                )
            
             # -----------------------------
            # Duplicate Candidate Validation
            # -----------------------------
            existing_candidate = (
                self.campaign_candidate_repo.get_by_campaign_and_candidate(
                    request.campaign_id,
                    request.candidate_id,
                )
            )

            if existing_candidate:
                raise CampaignException(
                    "Candidate already exists in this campaign.",
                    409,
                )


            # -----------------------------
            # Campaign must be ACTIVE
            # -----------------------------
            if campaign.status != CampaignStatus.ACTIVE:
                raise CampaignException(
                    "Campaign is closed. Resume uploads are not allowed.",
                    409,
                )

            # -----------------------------
            # Max Candidate Validation
            # -----------------------------
            current_count = (
            self.campaign_candidate_repo.get_candidate_count(
                request.campaign_id
            )
            )

            # --------------------------------------------------
            # Auto Close Campaign when Candidate Cap is Reached
            # --------------------------------------------------
            if (
                campaign.max_candidates
                and current_count >= campaign.max_candidates
            ):

                campaign.status = CampaignStatus.CLOSED

                self.campaign_repo.update(campaign)
                self.campaign_repo.commit()

                # Audit Log
                self.audit_service.log(
                    actor_id="SYSTEM",
                    actor_role="HR_ADMIN",
                    action_type=ActionType.CAMPAIGN_AUTO_CLOSED,
                    entity_type=EntityType.CAMPAIGN,
                    entity_id=campaign.id,
                    campaign_id=campaign.id,
                    details={
                        "reason": "Maximum candidate limit reached",
                        "max_candidates": campaign.max_candidates,
                        "current_candidates": current_count,
                    },
                )

                raise CampaignException(
                    "This campaign has reached its maximum candidate limit and is now closed.",
                    409,
                )

            # -----------------------------
            # Create Candidate
            # -----------------------------
            candidate = CampaignCandidate(
                campaign_id=request.campaign_id,
                candidate_id=request.candidate_id,
                resume_id=request.resume_id,
                idempotency_key=str(uuid.uuid4()),   # temporary placeholder
                pipeline_stage=PipelineStage.UPLOADED,
            )

            candidate = (
                self.campaign_candidate_repo.create(candidate)
            )

            self.campaign_candidate_repo.commit()

            self.audit_service.log(
            actor_id="SYSTEM",          # Later replace with logged-in user
            actor_role="HR_ADMIN",
            action_type=ActionType.CANDIDATE_ADDED,
            entity_type=EntityType.CAMPAIGN_CANDIDATE,
            entity_id=candidate.id,
            campaign_id=request.campaign_id,
            details={
                "candidate_id": str(request.candidate_id),
                "resume_id": str(request.resume_id),
                "pipeline_stage": candidate.pipeline_stage.value,
            },
        )

            return CampaignCandidateResponse.model_validate(
                candidate
            )

        except Exception:
            self.campaign_candidate_repo.rollback()
            raise

    def get_campaign_candidates(
        self,
        campaign_id: UUID,
    ) -> list[CampaignCandidateResponse]:
        """
        Get all candidates belonging to a campaign.
        """

        campaign = self.campaign_repo.get_by_id(campaign_id)

        if not campaign:
            raise CampaignException(
                "Campaign not found.",
                404,
            )

        candidates = (
            self.campaign_candidate_repo.get_all_by_campaign(
                campaign_id
            )
        )

        return [
            CampaignCandidateResponse.model_validate(candidate)
            for candidate in candidates
        ]

    def delete_campaign_candidate(
        self,
        campaign_candidate_id: UUID,
    ) -> None:
        """
        Delete a campaign candidate.
        """

        try:

            candidate = (
                self.campaign_candidate_repo.get_by_id(
                    campaign_candidate_id
                )
            )

            if not candidate:
                raise CampaignException(
                    "Campaign candidate not found.",
                    404,
                )

            self.campaign_candidate_repo.delete(candidate)

            self.campaign_candidate_repo.commit()

            # Audit Log
            self.audit_service.log(
                actor_id="SYSTEM",      # Replace with logged-in user later
                actor_role="HR_ADMIN",
                action_type=ActionType.CANDIDATE_REMOVED,
                entity_type=EntityType.CAMPAIGN_CANDIDATE,
                entity_id=candidate.id,
                campaign_id=candidate.campaign_id,
                details={
                    "candidate_id": str(candidate.candidate_id),
                    "resume_id": str(candidate.resume_id),
                },
            )

        except Exception:
            self.campaign_candidate_repo.rollback()
            raise