from uuid import UUID
import uuid

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

            if (
                campaign.max_candidates
                and current_count >= campaign.max_candidates
            ):
                raise CampaignException(
                    "Maximum candidate limit reached.",
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