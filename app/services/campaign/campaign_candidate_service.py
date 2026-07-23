import hashlib
import logging
from uuid import UUID

from app.core.encryption_service import DecryptionError, EncryptionService
from app.dependencies import campaign
from app.enums.constants import ActionType, EntityType
from app.exceptions.campaign_exceptions import CampaignException
from app.models.campaigns import CampaignStatus
from app.models.candidates import Candidate, Resume
from app.models.pipeline import (
    CampaignCandidate,
    PipelineStage,
    TransitionSource,
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

logger = logging.getLogger(__name__)

CANDIDATE_PII_PURPOSE = "CANDIDATE_PII"


class CampaignCandidateService:

    def __init__(
        self,
        campaign_repo: CampaignRepository,
        campaign_candidate_repo: CampaignCandidateRepository,
        audit_service: AuditService,
        encryption_service: EncryptionService | None = None,
    ):
        self.campaign_repo = campaign_repo
        self.campaign_candidate_repo = campaign_candidate_repo
        self.audit_service = audit_service
        self.encryption_service = encryption_service

    def create_campaign_candidate(
        self,
        request: CampaignCandidateCreateRequest,
        actor_id: str,
        actor_role: str | None = None,
    ) -> CampaignCandidateResponse:

        try:

            # -----------------------------
            # Validate Campaign
            # -----------------------------
            # Locked for the rest of this transaction (S05-T03): serializes
            # concurrent inserts against this campaign so the candidate-cap
            # check below can't race with another request's insert.
            campaign = self.campaign_repo.get_by_id_for_update(
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
            if campaign.status == CampaignStatus.PAUSED:
                # S01-T02: uploads are blocked immediately while paused, with a
                # message distinct from the closed case.
                raise CampaignException(
                    "This campaign is currently paused — uploads are not accepted.",
                    409,
                )
            if campaign.status != CampaignStatus.ACTIVE:
                raise CampaignException(
                    "This campaign is closed and no longer accepting applications.",
                    403,
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
                    actor_id=actor_id,
                    actor_role=actor_role,
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
            idempotency_key = self._build_idempotency_key(
                request.campaign_id, request.candidate_id, request.resume_id,
            )

            candidate = CampaignCandidate(
                campaign_id=request.campaign_id,
                candidate_id=request.candidate_id,
                resume_id=request.resume_id,
                idempotency_key=idempotency_key,
                pipeline_stage=PipelineStage.UPLOADED,
            )

            candidate, was_created = (
                self.campaign_candidate_repo.create_idempotent(candidate)
            )

            if not was_created:
                # A retried request under the same idempotency key (e.g. a
                # Celery task retry or a network-timeout resubmission) —
                # return the existing pipeline entry rather than writing a
                # second stage-history row or a duplicate audit entry.
                self.campaign_candidate_repo.commit()
                return CampaignCandidateResponse.model_validate(candidate)

            self.campaign_candidate_repo.create_stage_history(
                campaign_candidate_id=candidate.id,
                from_stage=None,
                to_stage=PipelineStage.UPLOADED,
                transition_source=TransitionSource.SYSTEM,
            )

            self.campaign_candidate_repo.commit()

            self.audit_service.log(
            actor_id=actor_id,
            actor_role=actor_role,
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

    @staticmethod
    def _build_idempotency_key(
        campaign_id: UUID,
        candidate_id: UUID,
        resume_id: UUID,
    ) -> str:
        raw = f"{campaign_id}:{candidate_id}:{resume_id}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get_campaign_candidates(
        self,
        campaign_id: UUID,
    ) -> list[CampaignCandidateResponse]:
        """
        Get all candidates belonging to a campaign, enriched for the
        Candidate Listing UI: decrypted candidate name, designation/
        experience parsed from the resume, and the scores already stored
        on CampaignCandidate (never recalculated here). location and
        risk_score have no backing data anywhere in the system yet, so
        they are always returned as null.
        """

        campaign = self.campaign_repo.get_by_id(campaign_id)

        if not campaign:
            raise CampaignException(
                "Campaign not found.",
                404,
            )

        rows = (
            self.campaign_candidate_repo.get_all_by_campaign(
                campaign_id
            )
        )

        return [
            self._to_campaign_candidate_response(campaign_candidate, candidate, resume)
            for campaign_candidate, candidate, resume in rows
        ]

    def _to_campaign_candidate_response(
        self,
        campaign_candidate: CampaignCandidate,
        candidate: Candidate | None,
        resume: Resume | None,
    ) -> CampaignCandidateResponse:
        designation, experience = self._extract_designation_and_experience(resume)

        return CampaignCandidateResponse(
            id=campaign_candidate.id,
            campaign_id=campaign_candidate.campaign_id,
            candidate_id=campaign_candidate.candidate_id,
            campaign_candidate_id=campaign_candidate.id,
            resume_id=campaign_candidate.resume_id,
            pipeline_stage=campaign_candidate.pipeline_stage,
            candidate_name=self._decrypt_candidate_name(candidate),
            current_designation=designation,
            experience=experience,
            deterministic_score=(
                float(campaign_candidate.deterministic_score)
                if campaign_candidate.deterministic_score is not None else None
            ),
            ai_ats_score=(
                float(campaign_candidate.ai_ats_score)
                if campaign_candidate.ai_ats_score is not None else None
            ),
            semantic_score=(
                float(campaign_candidate.semantic_score)
                if campaign_candidate.semantic_score is not None else None
            ),
            composite_score=(
                float(campaign_candidate.composite_score)
                if campaign_candidate.composite_score is not None else None
            ),
            location=None,
            risk_score=None,
            created_at=campaign_candidate.created_at,
        )

    def _decrypt_candidate_name(self, candidate: Candidate | None) -> str | None:
        if candidate is None or not candidate.full_name_encrypted:
            return None
        if self.encryption_service is None:
            logger.warning("No encryption_service configured - cannot decrypt candidate name.")
            return None
        try:
            return self.encryption_service.decrypt(
                candidate.full_name_encrypted, candidate.encryption_key_id,
            )
        except DecryptionError:
            logger.exception("Failed to decrypt candidate name for candidate_id=%s", candidate.id)
            return None

    @staticmethod
    def _extract_designation_and_experience(
        resume: Resume | None,
    ) -> tuple[str | None, float | None]:
        """
        Reads designation/experience straight out of the already-parsed
        resume JSON (ResumeExtractionResponse's shape) - never re-parses
        or re-extracts anything. designation prefers the work_experience
        entry marked is_current=True, falling back to the first (most
        recent) entry when none is marked current.
        """
        if resume is None or not resume.parsed_json:
            return None, None

        parsed = resume.parsed_json
        experience = parsed.get("total_experience_years")

        work_experience = parsed.get("work_experience") or []
        designation = None
        current_entry = next((entry for entry in work_experience if entry.get("is_current")), None)
        entry = current_entry or (work_experience[0] if work_experience else None)
        if entry:
            designation = entry.get("title")

        return designation, experience

    def delete_campaign_candidate(
        self,
        campaign_candidate_id: UUID,
        actor_id: str,
        actor_role: str | None = None,
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
                actor_id=actor_id,
                actor_role=actor_role,
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