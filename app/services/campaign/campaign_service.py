from datetime import datetime, timezone
from decimal import Decimal
from http.client import responses
from http.client import responses
from unicodedata import name
from uuid import UUID

from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.enums.constants import ActionType, EntityType
from app.exceptions.campaign_exceptions import CampaignException
from app.models.campaigns import CampaignStatus, HiringCampaign
from app.models.identity import User
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.jd_repository import JDRepository
from app.schemas.campaign.campaign_response import CampaignResponse
from app.schemas.campaign.campaign_schema import CampaignCreateRequest
from app.services.audit_service import AuditService


class CampaignService:

    def __init__(
        self,
        campaign_repo: CampaignRepository,
        jd_repo: JDRepository,
        audit_service: AuditService,
        db: Session,
    ):
        self.campaign_repo = campaign_repo
        self.jd_repo = jd_repo
        self.audit_service = audit_service
        self.db = db

    def  create_campaign(
        self,
        request: CampaignCreateRequest,
        org_id: UUID,
        created_by: str
    ) -> CampaignResponse:
        try:
            
            total_weight = request.weight_deterministic + request.weight_semantic + request.weight_ai
            if total_weight != Decimal("100.00"):
                raise CampaignException("Scoring weights must sum to 100.00", 422)

            jd = self.jd_repo.get_by_id(request.jd_id)
            if not jd:
                raise CampaignException(
                    "Invalid job description: Job description not found",
                    422
                )

            if not jd.is_active_version:
                raise CampaignException(
                    "Invalid job description: Job description is not the active version",
                    422
                )

            if jd.closed_at is not None:
                raise CampaignException(
                    "Invalid job description: Job description is closed",
                    422
                )


            existing_campaign = self.campaign_repo.get_by_name(org_id, request.name)
            if existing_campaign:
                raise CampaignException(
                    f"Campaign name '{existing_campaign.name}' already exists in this organization",
                    409)


            if request.deadline:
                if request.deadline <= datetime.now(timezone.utc):
                    raise CampaignException("Campaign deadline must be a future date", 422)

            campaign = HiringCampaign(
                org_id=org_id,
                jd_id=request.jd_id,
                name=request.name.strip(),
                status=CampaignStatus.ACTIVE,
                weight_deterministic=float(request.weight_deterministic),
                weight_semantic=float(request.weight_semantic),
                weight_ai=float(request.weight_ai),
                semantic_threshold=float(request.semantic_threshold),
                ai_threshold=float(request.ai_threshold),
                max_candidates=request.max_candidates,
                deadline=request.deadline,
                hiring_manager_id=request.hiring_manager_id,
                created_by=created_by,
            )

            
            campaign = self.campaign_repo.create_campaign(campaign)

            
            self.campaign_repo.commit()

            
            hiring_manager_name = request.hiring_manager_id | None
            # if campaign.hiring_manager_id:
            #     hiring_manager = self.db.query(User).filter(User.id == campaign.hiring_manager_id).first()
            #     if hiring_manager:
            #         hiring_manager_name = hiring_manager.full_name

            
            return CampaignResponse(
                id=campaign.id,
                name=campaign.name,
                status=campaign.status.value,
                jd_title=jd.title,
                jd_version=jd.version_number,
                hiring_manager=hiring_manager_name,
                created_at=campaign.created_at,
            )

        except Exception:
            self.campaign_repo.rollback()
            raise

    def get_campaign_by_id(self, campaign_id: UUID) -> CampaignResponse:

        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException(
                f"Campaign with ID '{campaign_id}' not found",
                404,
                None
            )

        jd = self.jd_repo.get_by_id(campaign.jd_id)
        if not jd:
            raise CampaignException(
                "Associated job description not found",
                404,
                None
            )

        hiring_manager_name = None
        # if campaign.hiring_manager_id:
        #     hiring_manager = self.db.query(User).filter(User.id == campaign.hiring_manager_id).first()
        #     if hiring_manager:
        #         hiring_manager_name = hiring_manager.full_name

        return CampaignResponse(
            id=campaign.id,
            name=campaign.name,
            status=campaign.status.value,
            jd_title=jd.title,
            jd_version=jd.version_number,
            hiring_manager=hiring_manager_name,
            created_at=campaign.created_at,
        )
    
    def get_all_campaigns(self, user: User) -> list[CampaignResponse]:
        campaigns = self.campaign_repo.get_all_campaigns()
        return [
            CampaignResponse(
                id=c.id,
                name=c.name,
                status=c.status.value,
                jd_title=c.job_description.title,
                jd_version=c.job_description.version_number,   # ← matches the actual column name
                hiring_manager=c.hiring_manager_id,
                deadline=c.deadline,
                created_at=c.created_at,
            )
            for c in campaigns
        ]
    
    def get_all_campaigns_for_hrAdmin(self, manager_id: UUID) -> list[CampaignResponse]:
        campaigns = self.campaign_repo.get_all_campaigns_for_hrAdmin(manager_id)
        return [
            CampaignResponse(
                id=c.id,
                name=c.name,
                status=c.status.value,
                jd_title=c.job_description.title,
                jd_version=c.job_description.version_number,
                max_candidates=c.max_candidates,
                hiring_manager=c.hiring_manager_id,
                deadline=c.deadline,
                created_at=c.created_at,
            )
            for c in campaigns
        ]
    
    def get_all_campaigns_for_hiring_manager(self, manager_id: UUID) -> list[CampaignResponse]:
        campaigns = self.campaign_repo.get_all_campaigns_for_hiring_manager(manager_id)
        return [
            CampaignResponse(
                id=c.id,
                name=c.name,
                status=c.status.value,
                jd_title=c.job_description.title,
                jd_version=c.job_description.version_number,
                hiring_manager=c.hiring_manager_id,
                deadline=c.deadline,
                created_at=c.created_at,
            )
            for c in campaigns
        ]
