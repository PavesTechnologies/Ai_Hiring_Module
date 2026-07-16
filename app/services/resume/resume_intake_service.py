from uuid import UUID, uuid4

from app.enums.constants import ActionType, EntityType
from app.exceptions.campaign_exceptions import CampaignException
from app.models.campaigns import CampaignStatus, HiringCampaign
from app.models.candidates import Resume
from app.repositories.CampaignRepository import CampaignRepository
from app.schemas.campaign.campaign_candidate_schema import (
    CampaignCandidateCreateRequest,
    CampaignCandidateResponse,
)
from app.services.audit_service import AuditService
from app.services.campaign.campaign_candidate_service import CampaignCandidateService
from app.services.resume.resume_service import ResumeService
from app.tasks.resume_processing_tasks import process_resume_document

UPLOAD_CONSENT_SOURCE = "UPLOAD_FORM"


class ResumeIntakeService:
    """
    Orchestrates the individual-resume-upload endpoint: a cheap campaign
    pre-check (fail fast before storing anything), the actual upload
    (Phase 5), the authoritative, race-safe campaign-candidate insert
    (Phase 6's hardened CampaignCandidateService), and enqueuing the
    RESUME_PARSE background task (Phase 8) to actually parse the file.
    """

    def __init__(
        self,
        resume_service: ResumeService,
        campaign_candidate_service: CampaignCandidateService,
        campaign_repo: CampaignRepository,
        audit_service: AuditService,
    ):
        self.resume_service = resume_service
        self.campaign_candidate_service = campaign_candidate_service
        self.campaign_repo = campaign_repo
        self.audit_service = audit_service

    def upload_resume(
        self,
        campaign_id: UUID,
        file_bytes: bytes,
        filename: str,
        candidate_full_name: str,
        candidate_email: str,
        jurisdiction: str,
        uploaded_by: str,
        actor_role: str | None = None,
        content_type: str | None = None,
        candidate_phone: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[Resume, CampaignCandidateResponse, HiringCampaign, UUID]:
        campaign = self._precheck_campaign_eligibility(campaign_id)

        resume = self.resume_service.upload(
            file_bytes=file_bytes,
            filename=filename,
            candidate_full_name=candidate_full_name,
            candidate_email=candidate_email,
            jurisdiction=jurisdiction,
            consent_source=UPLOAD_CONSENT_SOURCE,
            uploaded_by=uploaded_by,
            content_type=content_type,
            candidate_phone=candidate_phone,
            source_campaign_id=campaign_id,
            ip_address=ip_address,
            user_agent=user_agent,
        )

        campaign_candidate = self.campaign_candidate_service.create_campaign_candidate(
            CampaignCandidateCreateRequest(
                campaign_id=campaign_id,
                candidate_id=resume.candidate_id,
                resume_id=resume.id,
            ),
            actor_id=uploaded_by,
            actor_role=actor_role,
        )

        try:
            self.audit_service.log(
                actor_id=uploaded_by,
                actor_role=None,
                action_type=ActionType.RESUME_UPLOADED,
                entity_type=EntityType.RESUME,
                entity_id=resume.id,
                campaign_id=campaign_id,
                details={
                    "candidate_id": str(resume.candidate_id),
                    "file_format": resume.file_format.value,
                },
            )
            self.campaign_repo.commit()
        except Exception:
            self.campaign_repo.rollback()
            raise

        task_id = uuid4()
        process_resume_document.apply_async(
            kwargs={
                "task_id": str(task_id),
                "resume_id": str(resume.id),
                "candidate_id": str(resume.candidate_id),
                "file_path": resume.file_path,
                "file_format": resume.file_format.value,
            },
            task_id=str(task_id),
        )

        return resume, campaign_candidate, campaign, task_id

    def _precheck_campaign_eligibility(self, campaign_id: UUID) -> HiringCampaign:
        """
        Fast, non-authoritative check so an obviously-doomed request fails
        before a file is uploaded to storage. Not a substitute for the
        locked, race-safe check inside
        CampaignCandidateService.create_campaign_candidate, which still
        runs afterward as the source of truth.
        """
        campaign = self.campaign_repo.get_by_id(campaign_id)

        if not campaign:
            raise CampaignException("Campaign not found.", 404)

        if campaign.status == CampaignStatus.PAUSED:
            raise CampaignException(
                "This campaign is currently paused — uploads are not accepted.", 409,
            )
        if campaign.status != CampaignStatus.ACTIVE:
            raise CampaignException(
                "Campaign is closed. Resume uploads are not allowed.", 409,
            )

        if campaign.max_candidates:
            current_count = self.campaign_repo.get_candidate_count(campaign_id)
            if current_count >= campaign.max_candidates:
                raise CampaignException(
                    "This campaign has reached its maximum candidate limit.", 409,
                )

        return campaign
