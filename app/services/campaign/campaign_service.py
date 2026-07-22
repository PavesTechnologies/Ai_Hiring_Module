from datetime import datetime, timezone
from decimal import Decimal
from urllib import request
from fastapi import HTTPException
from uuid import UUID
from datetime import timedelta
from app.middleware.rbac import TokenUser

from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.enums.constants import ActionType, EntityType, UserRole
from app.exceptions.campaign_exceptions import CampaignException
from app.models.campaign_weight_preset import CampaignWeightPreset
from app.models.campaigns import CampaignStatus, HiringCampaign
from app.models.identity import User
from app.models.identity import UserRole as LocalUserRole
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.jd_repository import JDRepository
from app.schemas.campaign.campaign_filter_schema import CampaignFilterRequest
from app.schemas.campaign.campaign_response import CampaignResponse, CampaignScoringConfigurationResponse, CampaignScoringDefaultsResponse, ScoringLayerExplanationResponse
from app.schemas.campaign.campaign_schema import CampaignCreateRequest, CampaignUpdateRequest, CampaignScoringUpdateRequest
from app.schemas.campaign.campaign_weight_preset_schema import CampaignWeightPresetCreateRequest, CampaignWeightPresetResponse, CampaignWeightPresetUpdateRequest
from app.services.audit_service import AuditService
from app.schemas.campaign.campaign_pause_schema import PauseImpactSummaryResponse, ResumeSummaryResponse
from app.schemas.campaign.campaign_response import (
    CampaignWeightHistoryResponse,
    WeightHistoryItemResponse,
)
from app.repositories.campaign_weight_preset_repository import (
    CampaignWeightPresetRepository,
)
from app.schemas.campaign.campaign_detail_response import (
    CampaignDetailResponse,
    CampaignInfoSection,
    JDConfigSection,
    ScoringConfigSection,
    PipelineLimitsSection,
    HiringManagerSection,
)
from app.models.pipeline import PipelineStage
from app.schemas.campaign.pipeline_summary_response import PipelineSummaryResponse, StageStat
from app.schemas.campaign.campaign_timeline_response import CampaignTimelineResponse, TimelineEntry


class CampaignService:

    def __init__(
        self,
        campaign_repo: CampaignRepository,
        jd_repo: JDRepository,
        audit_service: AuditService,
        config_repo: ConfigRepository,
        preset_repo: CampaignWeightPresetRepository,
        db: Session,

    ):
        self.campaign_repo = campaign_repo
        self.jd_repo = jd_repo
        self.audit_service = audit_service
        self.config_repo = config_repo
        self.preset_repo = preset_repo
        self.db = db

    def _get_warning_thresholds(self) -> tuple[float, int]:
        """
        S04-T03: cap/deadline warning thresholds, sourced from platform_config
        (CAP_WARNING_PERCENTAGE / DEADLINE_WARNING_DAYS) with the previous
        hardcoded values (80%, 3 days) as fallback if the keys aren't seeded.
        """
        configs = self.config_repo.get_configs_by_keys(
            ["CAP_WARNING_PERCENTAGE", "DEADLINE_WARNING_DAYS"]
        )
        cap_warning_percentage = float(configs.get("CAP_WARNING_PERCENTAGE", "80.00"))
        deadline_warning_days = int(configs.get("DEADLINE_WARNING_DAYS", "3"))
        return cap_warning_percentage, deadline_warning_days

    def _is_approaching_cap(
        self,
        candidate_count: int,
        max_candidates: int | None,
        warning_percentage: float = 80.0,
    ) -> bool:
        """
        Returns True if campaign has reached warning_percentage of its candidate cap.
        """
        if not max_candidates:
            return False

        return candidate_count >= (max_candidates * (warning_percentage / 100))


    def _is_deadline_soon(
        self,
        deadline: datetime | None,
        warning_days: int = 3,
    ) -> bool:
        """
        Returns True if campaign deadline is within the warning period.
        """
        if deadline is None:
            return False

        now = datetime.now(timezone.utc)

        return now <= deadline <= now + timedelta(days=warning_days)

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
                deterministic_threshold=float(request.deterministic_threshold),
                max_candidates=request.max_candidates,
                deadline=request.deadline,
                hiring_manager_id=request.hiring_manager_id,
                recruiter_id=request.recruiter_id,
                created_by=created_by,
            )

            
            campaign = self.campaign_repo.create_campaign(campaign)

            # Same transaction as the campaign itself: rolled back together on
            # failure. campaign_id is what the activity timeline filters on.
            self.audit_service.log(
                actor_id=created_by,
                actor_role="HR_ADMIN",
                action_type=ActionType.CAMPAIGN_CREATED,
                entity_type=EntityType.CAMPAIGN,
                entity_id=campaign.id,
                campaign_id=campaign.id,
                details={
                    "title": f"Campaign '{campaign.name}' created",
                    "jd_id": str(campaign.jd_id),
                },
            )

            self.campaign_repo.commit()


            hiring_manager_name = request.hiring_manager_id
            # if campaign.hiring_manager_id:
            #     hiring_manager = self.db.query(User).filter(User.id == campaign.hiring_manager_id).first()
            #     if hiring_manager:
            #         hiring_manager_name = hiring_manager.full_name

            cap_warning_percentage, deadline_warning_days = self._get_warning_thresholds()
            candidate_count = self.campaign_repo.get_candidate_count(campaign.id)

            return CampaignResponse(
                id=campaign.id,
                name=campaign.name,
                status=campaign.status.value,
                jd_title=jd.title,
                jd_version=jd.version_number,
                hiring_manager=hiring_manager_name,
                max_candidates=campaign.max_candidates,
                deadline=campaign.deadline,
                created_at=campaign.created_at,
                candidate_count=candidate_count,
                shortlisted_count=self.campaign_repo.get_shortlisted_count(campaign.id),
                approaching_cap=self._is_approaching_cap(
                    candidate_count,
                    campaign.max_candidates,
                    cap_warning_percentage,
                ),
                deadline_soon=self._is_deadline_soon(
                    campaign.deadline,
                    deadline_warning_days,
                )
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
        if campaign.hiring_manager_id:
            hiring_manager = self.db.query(User).filter(User.id == campaign.hiring_manager_id).first()
            if hiring_manager:
                hiring_manager_name = hiring_manager.full_name

        cap_warning_percentage, deadline_warning_days = self._get_warning_thresholds()
        candidate_count = self.campaign_repo.get_candidate_count(campaign.id)

        return CampaignResponse(
            id=campaign.id,
            name=campaign.name,
            status=campaign.status.value,
            jd_title=jd.title,
            jd_version=jd.version_number,
            hiring_manager=hiring_manager_name,
            max_candidates=campaign.max_candidates,
            deadline=campaign.deadline,
            created_at=campaign.created_at,
            candidate_count=candidate_count,
            shortlisted_count=self.campaign_repo.get_shortlisted_count(campaign.id),
            approaching_cap=self._is_approaching_cap(
                candidate_count,
                campaign.max_candidates,
                cap_warning_percentage,
            ),
            deadline_soon=self._is_deadline_soon(
                campaign.deadline,
                deadline_warning_days,
            )
        )

    def get_scoring_configuration(
        self,
        campaign_id: UUID,
    ) -> CampaignScoringConfigurationResponse:

        campaign = self.campaign_repo.get_by_id(campaign_id)

        if not campaign:
            raise CampaignException(
                f"Campaign with ID '{campaign_id}' not found",
                404,
                None,
            )

        configs = self.config_repo.get_configs_by_keys(
            [
                "DEFAULT_WEIGHT_DETERMINISTIC",
                "DEFAULT_WEIGHT_SEMANTIC",
                "DEFAULT_WEIGHT_AI",
                "DEFAULT_SEMANTIC_THRESHOLD",
                "DEFAULT_AI_THRESHOLD",
            ]
        )
        formula = "((det × w_det) + (sem × 100 × w_sem) + (eff_ai × w_ai)) / 100"
        layers = [
            ScoringLayerExplanationResponse(
                layer="Deterministic",
                weight=campaign.weight_deterministic,
                threshold=campaign.deterministic_threshold,
                description="Mandatory skill, experience and education validation.",
                ),
                ScoringLayerExplanationResponse(
                    layer="Semantic",
                    weight=campaign.weight_semantic,
                    threshold=campaign.semantic_threshold,
                    description="Contextual similarity between Job Description and Resume.",
                ),
                ScoringLayerExplanationResponse(
                    layer="AI Evaluation",
                    weight=campaign.weight_ai,
                    threshold=campaign.ai_threshold,
                    description="LLM generated ATS evaluation score.",
                ),
            ]
        

        total_weight = (
            campaign.weight_deterministic
            + campaign.weight_semantic
            + campaign.weight_ai
        )

        return CampaignScoringConfigurationResponse(
            weight_deterministic=campaign.weight_deterministic,
            weight_semantic=campaign.weight_semantic,
            weight_ai=campaign.weight_ai,
            semantic_threshold=campaign.semantic_threshold,
            ai_threshold=campaign.ai_threshold,
            deterministic_threshold=campaign.deterministic_threshold,
            total_weight=total_weight,
            formula=formula,
            layers=layers,
            defaults=CampaignScoringDefaultsResponse(
                # NOTE: platform_config has no DEFAULT_WEIGHT_* rows yet (only
                # SEMANTIC_PASS_THRESHOLD / AI_PASS_THRESHOLD exist, under the
                # names S02 already uses). Falling back to the HiringCampaign
                # column defaults — same values — instead of crashing with a
                # KeyError until the platform_config keys are decided/seeded.
                weight_deterministic=float(
                    configs.get("DEFAULT_WEIGHT_DETERMINISTIC", "30.00")
                ),
                weight_semantic=float(
                    configs.get("DEFAULT_WEIGHT_SEMANTIC", "40.00")
                ),
                weight_ai=float(
                    configs.get("DEFAULT_WEIGHT_AI", "30.00")
                ),
                semantic_threshold=float(
                    configs.get("DEFAULT_SEMANTIC_THRESHOLD", "0.6500")
                ),
                ai_threshold=float(
                    configs.get("DEFAULT_AI_THRESHOLD", "50.00")
                ),
            ),
        )
    
    def get_scoring_history(
        self,
        campaign_id: UUID,
    ) -> CampaignWeightHistoryResponse:

        campaign = self.campaign_repo.get_by_id(campaign_id)

        if not campaign:
            raise CampaignException(
                f"Campaign with ID '{campaign_id}' not found",
                404,
                None,
            )

        history = self.audit_service.get_campaign_scoring_history(
            campaign_id
        )

        history_items = []

        for record in history:

            detail = record.detail or {}

            history_items.append(
                WeightHistoryItemResponse(
                    changed_by=str(record.actor_id),
                    changed_at=record.created_at,
                    before=detail.get("before", {}),
                    after=detail.get("after", {}),
                )
            )

        return CampaignWeightHistoryResponse(
            history=history_items
        )
    def get_all_campaigns(self, user: User, show_closed: bool = False) -> list[CampaignResponse]:
        campaigns = self.campaign_repo.get_all_campaigns(show_closed=show_closed)
        cap_warning_percentage, deadline_warning_days = self._get_warning_thresholds()
        return [
            CampaignResponse(
                id=c.id,
                name=c.name,
                status=c.status.value,
                jd_title=c.job_description.title,
                jd_version=c.job_description.version_number,   # ← matches the actual column name
                hiring_manager=c.hiring_manager_id,
                max_candidates=c.max_candidates,
                deadline=c.deadline,
                created_at=c.created_at,
                candidate_count=self.campaign_repo.get_candidate_count(c.id),
                shortlisted_count=self.campaign_repo.get_shortlisted_count(c.id),
                approaching_cap=self._is_approaching_cap(
                    self.campaign_repo.get_candidate_count(c.id),
                    c.max_candidates,
                    cap_warning_percentage,
                ),
                deadline_soon=self._is_deadline_soon(
                    c.deadline,
                    deadline_warning_days,
                )

            )
            for c in campaigns
        ]

    def get_all_campaigns_for_hrAdmin(self, manager_id: UUID) -> list[CampaignResponse]:
        campaigns = self.campaign_repo.get_all_campaigns_for_hrAdmin(manager_id)
        cap_warning_percentage, deadline_warning_days = self._get_warning_thresholds()
        return [
            CampaignResponse(
                id=c.id,
                name=c.name,
                status=c.status.value,
                jd_title=c.job_description.title,
                jd_version=c.job_description.version_number,
                hiring_manager=c.hiring_manager_id,
                max_candidates=c.max_candidates,
                deadline=c.deadline,
                created_at=c.created_at,
                candidate_count=self.campaign_repo.get_candidate_count(c.id),
                shortlisted_count=self.campaign_repo.get_shortlisted_count(c.id),
                approaching_cap=self._is_approaching_cap(
                    self.campaign_repo.get_candidate_count(c.id),
                    c.max_candidates,
                    cap_warning_percentage,
                ),
                deadline_soon=self._is_deadline_soon(
                    c.deadline,
                    deadline_warning_days,
                )
            )
            for c in campaigns
        ]

    def get_all_campaigns_for_hiring_manager(self, manager_id: UUID) -> list[CampaignResponse]:
        campaigns = self.campaign_repo.get_all_campaigns_for_hiring_manager(manager_id)
        cap_warning_percentage, deadline_warning_days = self._get_warning_thresholds()
        return [
            CampaignResponse(
                id=c.id,
                name=c.name,
                status=c.status.value,
                jd_title=c.job_description.title,
                jd_version=c.job_description.version_number,
                hiring_manager=c.hiring_manager_id,
                max_candidates=c.max_candidates,
                deadline=c.deadline,
                created_at=c.created_at,
                candidate_count=self.campaign_repo.get_candidate_count(c.id),
                shortlisted_count=self.campaign_repo.get_shortlisted_count(c.id),
                approaching_cap=self._is_approaching_cap(
                    self.campaign_repo.get_candidate_count(c.id),
                    c.max_candidates,
                    cap_warning_percentage,
                ),
                deadline_soon=self._is_deadline_soon(
                    c.deadline,
                    deadline_warning_days,
                )
            )
            for c in campaigns
        ]


    def search_campaigns(
        self,
        filters: CampaignFilterRequest,
    ) -> list[CampaignResponse]:

        campaigns = self.campaign_repo.search_campaigns(filters)
        cap_warning_percentage, deadline_warning_days = self._get_warning_thresholds()

        return [
            CampaignResponse(
                id=c.id,
                name=c.name,
                status=c.status.value,
                jd_title=c.job_description.title,
                jd_version=c.job_description.version_number,
                hiring_manager=c.hiring_manager_id,
                deadline=c.deadline,
                max_candidates=c.max_candidates,
                created_at=c.created_at,
                candidate_count=self.campaign_repo.get_candidate_count(c.id),
                shortlisted_count=self.campaign_repo.get_shortlisted_count(c.id),
                approaching_cap=self._is_approaching_cap(
                    self.campaign_repo.get_candidate_count(c.id),
                    c.max_candidates,
                    cap_warning_percentage,
                ),
                deadline_soon=self._is_deadline_soon(
                    c.deadline,
                    deadline_warning_days,
                )
            )
            for c in campaigns
        ]

    def update_scoring_configuration(
        self,
        campaign_id: UUID,
        request: CampaignScoringUpdateRequest,
        updated_by: str,
    ) -> CampaignScoringConfigurationResponse:

        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException(
                f"Campaign with ID '{campaign_id}' not found",
                404,
                None,
            )

        total_weight = (
            request.weight_deterministic
            + request.weight_semantic
            + request.weight_ai
        )

        if total_weight != Decimal("100.00"):
            raise CampaignException(
                "Total scoring weight must equal 100%.",
                400,
                None,
            )

        configs = self.config_repo.get_configs_by_keys(
            [
                "MIN_LAYER_WEIGHT",
            ]
        )

        min_layer_weight = Decimal(
            configs.get(
                "MIN_LAYER_WEIGHT",
                "5.00",
            )
        )

        if (
            request.weight_deterministic < min_layer_weight
            or request.weight_semantic < min_layer_weight
            or request.weight_ai < min_layer_weight
        ):
            raise CampaignException(
                f"Each scoring layer must be at least {min_layer_weight}%.",
                400,
                None,
            )

        # T03: capture before/after for every field that actually changed,
        # atomically with the save (audit is written in the same transaction).
        threshold_fields = (
            "weight_deterministic", "weight_semantic", "weight_ai",
            "semantic_threshold", "ai_threshold",
        )
        changes = {
            field: {
                "before": str(getattr(campaign, field)),
                "after": str(getattr(request, field)),
            }
            for field in threshold_fields
            if Decimal(str(getattr(campaign, field))) != getattr(request, field)
        }

        candidate_count = self.campaign_repo.get_candidate_count(campaign.id)

        campaign = (
            self.campaign_repo.update_scoring_configuration(
                campaign,
                request,
            )
        )

        if changes:
            self.audit_service.log(
                actor_id=updated_by,
                actor_role="HR_ADMIN",
                action_type=ActionType.CAMPAIGN_THRESHOLDS_UPDATED,
                entity_type=EntityType.CAMPAIGN,
                entity_id=campaign.id,
                campaign_id=campaign.id,
                details={
                    "title": f"Campaign '{campaign.name}' thresholds updated",
                    "changes": changes,
                    "candidates_already_processed": candidate_count,
                },
            )

        self.campaign_repo.commit()

        result = self.get_scoring_configuration(campaign.id)

        if candidate_count > 0:
            # T03: "a warning must notify HR_ADMIN that threshold changes only
            # affect newly submitted candidates" — surfaced on the response.
            result.warning = (
                f"{candidate_count} candidate(s) were already processed with "
                f"the previous configuration. Their scores will not be "
                f"automatically recalculated."
            )

        return result
    
    def get_weight_presets(
        self,
        org_id: UUID,
    ) -> list[CampaignWeightPresetResponse]:

        system_presets = [
            CampaignWeightPresetResponse(
                id=UUID("00000000-0000-0000-0000-000000000001"),
                name="Technical Role",
                description="Emphasises skill matching.",
                weight_deterministic=Decimal("40.00"),
                weight_semantic=Decimal("40.00"),
                weight_ai=Decimal("20.00"),
                deterministic_threshold=Decimal("70.00"),
                semantic_threshold=Decimal("65.00"),
                ai_threshold=Decimal("50.00"),
                created_by="SYSTEM",
                created_at=datetime.now(timezone.utc),
            ),
            CampaignWeightPresetResponse(
                id=UUID("00000000-0000-0000-0000-000000000002"),
                name="Managerial Role",
                description="Emphasises AI reasoning.",
                weight_deterministic=Decimal("20.00"),
                weight_semantic=Decimal("30.00"),
                weight_ai=Decimal("50.00"),
                deterministic_threshold=Decimal("70.00"),
                semantic_threshold=Decimal("65.00"),
                ai_threshold=Decimal("50.00"),
                created_by="SYSTEM",
                created_at=datetime.now(timezone.utc),
            ),
            CampaignWeightPresetResponse(
                id=UUID("00000000-0000-0000-0000-000000000003"),
                name="Balanced",
                description="Platform default.",
                weight_deterministic=Decimal("30.00"),
                weight_semantic=Decimal("40.00"),
                weight_ai=Decimal("30.00"),
                deterministic_threshold=Decimal("70.00"),
                semantic_threshold=Decimal("65.00"),
                ai_threshold=Decimal("50.00"),
                created_by="SYSTEM",
                created_at=datetime.now(timezone.utc),
            ),
            CampaignWeightPresetResponse(
                id=UUID("00000000-0000-0000-0000-000000000004"),
                name="Entry Level",
                description="Emphasises contextual fit.",
                weight_deterministic=Decimal("20.00"),
                weight_semantic=Decimal("50.00"),
                weight_ai=Decimal("30.00"),
                deterministic_threshold=Decimal("70.00"),
                semantic_threshold=Decimal("65.00"),
                ai_threshold=Decimal("50.00"),
                created_by="SYSTEM",
                created_at=datetime.now(timezone.utc),
            ),
        ]

        custom_presets = self.preset_repo.get_all_by_org(
            org_id
        )

        preset_responses = [
            CampaignWeightPresetResponse.model_validate(
                preset
            )
            for preset in custom_presets
        ]

        return system_presets + preset_responses
    
    def create_weight_preset(
        self,
        request: CampaignWeightPresetCreateRequest,
        org_id: UUID,
        created_by: str,
    ) -> CampaignWeightPresetResponse:

        existing_preset = self.preset_repo.get_by_name(
            org_id=org_id,
            name=request.name,
        )

        if existing_preset:
            raise CampaignException(
                f"Preset '{request.name}' already exists.",
                400,
                None,
            )

        total_weight = (
            request.weight_deterministic
            + request.weight_semantic
            + request.weight_ai
        )

        if total_weight != Decimal("100.00"):
            raise CampaignException(
                "Total scoring weight must equal 100.",
                400,
                None,
            )

        preset = CampaignWeightPreset(
            org_id=org_id,
            name=request.name.strip(),
            description=request.description,
            weight_deterministic=request.weight_deterministic,
            weight_semantic=request.weight_semantic,
            weight_ai=request.weight_ai,
            deterministic_threshold=request.deterministic_threshold,
            semantic_threshold=request.semantic_threshold,
            ai_threshold=request.ai_threshold,
            created_by=created_by,
        )

        preset = self.preset_repo.create(preset)

        self.preset_repo.commit()

        self.audit_service.log(
            actor_id=created_by,
            actor_role=None,
            action_type=ActionType.CAMPAIGN_SCORING_CONFIG_CHANGED.value,
            entity_type=EntityType.CAMPAIGN.value,
            entity_id=preset.id,
            details={
                "message": f"Created campaign weight preset '{preset.name}'"
            },
            campaign_id=None,
            jurisdiction=None,
            ip_address=None,
            session_id=None,
            request_id=None,
        )
        self.audit_service.repository.save()
        return CampaignWeightPresetResponse.model_validate(
            preset
        )
    
    def update_weight_preset(
        self,
        preset_id: UUID,
        request: CampaignWeightPresetUpdateRequest,
        org_id: UUID,
        updated_by: str,
    ) -> CampaignWeightPresetResponse:
        

        preset = self.preset_repo.get_by_id(
            preset_id
        )

        if not preset:
            raise CampaignException(
                "Weight preset not found.",
                404,
                None,
            )

        if preset.org_id != org_id:
            raise CampaignException(
                "Weight preset not found.",
                404,
                None,
            )

        duplicate = self.preset_repo.get_by_name(
            org_id=org_id,
            name=request.name,
        )

        if duplicate and duplicate.id != preset.id:
            raise CampaignException(
                f"Preset '{request.name}' already exists.",
                400,
                None,
            )

        total_weight = (
            request.weight_deterministic
            + request.weight_semantic
            + request.weight_ai
        )

        if total_weight != Decimal("100.00"):
            raise CampaignException(
                "Total scoring weight must equal 100.",
                400,
                None,
            )

        preset.name = request.name.strip()
        preset.description = request.description
        preset.weight_deterministic = request.weight_deterministic
        preset.weight_semantic = request.weight_semantic
        preset.weight_ai = request.weight_ai
        preset.semantic_threshold = request.semantic_threshold
        preset.ai_threshold = request.ai_threshold

        preset = self.preset_repo.update(
            preset
        )

        self.preset_repo.commit()

        self.audit_service.log(
            actor_id=updated_by,
            actor_role=None,
            action_type=ActionType.CAMPAIGN_SCORING_CONFIG_CHANGED.value,
            entity_type=EntityType.CAMPAIGN.value,
            entity_id=preset.id,
            details={
                "message": f"Updated preset '{preset.name}'"
            },
        )

        self.audit_service.repository.save()

        return CampaignWeightPresetResponse.model_validate(
            preset
        )
    
    def delete_weight_preset(
        self,
        preset_id: UUID,
        org_id: UUID,
        deleted_by: str,
    ) -> None:

        preset = self.preset_repo.get_by_id(
            preset_id
        )

        if not preset:
            raise CampaignException(
                "Weight preset not found.",
                404,
                None,
            )

        if preset.org_id != org_id:
            raise CampaignException(
                "Weight preset not found.",
                404,
                None,
            )

        self.preset_repo.delete(
            preset
        )

        self.preset_repo.commit()

        self.audit_service.log(
            actor_id=deleted_by,
            actor_role=None,
            action_type=ActionType.CAMPAIGN_SCORING_CONFIG_CHANGED.value,
            entity_type=EntityType.CAMPAIGN.value,
            entity_id=preset.id,
            details={
                "message": f"Deleted preset '{preset.name}'"
            },
        )

        self.audit_service.repository.save()

    def get_campaign_details(self,campaign_id: UUID, user:TokenUser) -> CampaignDetailResponse:
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException(f"Campaign '{campaign_id}' not found",404)

        jd = self.jd_repo.get_by_id(campaign.jd_id)
        if not jd:
            raise CampaignException("Associated job description not found", 404)

        creator = self.campaign_repo.get_user(campaign.created_by)
        manager = (
            self.campaign_repo.get_user(campaign.hiring_manager_id)
            if campaign.hiring_manager_id
            else None
        )

        is_hiring_manager_only = (
            UserRole.HIRING_MANAGER.value in user.roles
            and UserRole.HR_ADMIN.value not in user.roles
            and UserRole.RECRUITER.value not in user.roles
        )

        return CampaignDetailResponse(
            id=campaign.id,
            campaign_info=CampaignInfoSection(
                name=campaign.name,
                status=campaign.status.value,
                created_by_name=creator.full_name if creator else None,
                created_at=campaign.created_at,
                updated_at=campaign.updated_at,
            ),
            jd_configuration=JDConfigSection(
                jd_id=jd.id,
                jd_title=jd.title,
                version_number=jd.version_number,
                jurisdiction=jd.jurisdiction,
                mandatory_skill_count=self.campaign_repo.get_mandatory_skill_count(jd.id),
            ),
            # role gate: spec says HM must NOT see weights or manager contact
            scoring_configuration=None if is_hiring_manager_only else ScoringConfigSection(
                weight_deterministic=campaign.weight_deterministic,
                weight_semantic=campaign.weight_semantic,
                weight_ai=campaign.weight_ai,
                semantic_threshold=campaign.semantic_threshold,
                ai_threshold=campaign.ai_threshold,
                deterministic_threshold=campaign.deterministic_threshold,
            ),
            pipeline_limits=PipelineLimitsSection(
                max_candidates=campaign.max_candidates,
                current_candidate_count=self.campaign_repo.get_candidate_count(campaign.id),
                deadline=campaign.deadline,
            ),
            hiring_manager=(None if is_hiring_manager_only else (HiringManagerSection(
                full_name=manager.full_name,
                email=manager.email,
            ) if manager else None)),
        )

    # The true sequential funnel: a candidate normally passes through these in
    # order, so "drop-off between each stage" is meaningful here.
    _FUNNEL_STAGES = (
        PipelineStage.UPLOADED,
        PipelineStage.SCREENING,
        PipelineStage.SHORTLISTED,
        PipelineStage.HM_REVIEW,
        PipelineStage.INTERVIEW,
        PipelineStage.SELECTED,
    )
    # Side buckets a candidate can land in from any funnel stage, not a "next
    # step" after the one before it — comparing counts across these produces
    # meaningless percentages (e.g. REJECTED vs SELECTED), so they're reported
    # with a count only, no drop_off_pct.
    _BUCKET_STAGES = (
        PipelineStage.HOLD,
        PipelineStage.REJECTED,
        PipelineStage.FRAUD_REVIEW,
    )

    def get_pipeline_summary(self, campaign_id: UUID) -> PipelineSummaryResponse:
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException(f"Campaign '{campaign_id}' not found", 404)

        counts = self.campaign_repo.get_stage_counts(campaign_id)

        stages: list[StageStat] = []
        prev_count = None

        for stage in self._FUNNEL_STAGES:
            count = counts.get(stage.value, 0)

            drop_off = None
            if prev_count is not None and prev_count > 0:
                drop_off = round((prev_count - count) / prev_count * 100, 1)

            stages.append(StageStat(stage=stage.value, count=count, drop_off_pct=drop_off))
            prev_count = count

        for stage in self._BUCKET_STAGES:
            stages.append(StageStat(stage=stage.value, count=counts.get(stage.value, 0), drop_off_pct=None))

        return PipelineSummaryResponse(
            campaign_id=campaign_id,
            total_candidates=sum(counts.values()),
            stages=stages,
        )

    def get_campaign_timeline(
        self,
        campaign_id: UUID,
        limit: int = 20,
        offset: int = 0,
        event_type: str | None = None,
    ) -> CampaignTimelineResponse:
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException(f"Campaign '{campaign_id}' not found", 404)

        entries: list[TimelineEntry] = []

        for log in self.campaign_repo.get_audit_entries(campaign_id):
            detail = log.detail or {}
            entries.append(TimelineEntry(
                timestamp=log.created_at,
                event_type=log.action_type.value,
                actor_name=self._resolve_actor(log.actor_id),
                description=detail.get("title") or log.action_type.value.replace("_", " ").title(),
            ))

        for h in self.campaign_repo.get_stage_history(campaign_id):
            from_stage = h.from_stage.value if h.from_stage else "START"
            entries.append(TimelineEntry(
                timestamp=h.changed_at,
                event_type=f"CANDIDATE_{h.to_stage.value}",
                actor_name=self._resolve_actor(h.changed_by),
                description=f"Candidate moved {from_stage} → {h.to_stage.value}",
            ))

        for job in self.campaign_repo.get_bulk_upload_events(campaign_id):
            if job.status.value == "COMPLETED":
                job_event = "BULK_UPLOAD_COMPLETED"
                summary = f"Bulk upload '{job.original_filename}' completed — {job.processed_count}/{job.total_files} processed"
            elif job.status.value in ("FAILED", "PARTIAL_FAILURE"):
                job_event = "BULK_UPLOAD_FAILED"
                summary = f"Bulk upload '{job.original_filename}' failed — {job.failed_count}/{job.total_files} failed"
            else:
                job_event = "BULK_UPLOAD_STARTED"
                summary = f"Bulk upload '{job.original_filename}' started — {job.total_files} files"

            entries.append(TimelineEntry(
                timestamp=job.completed_at or job.created_at,
                event_type=job_event,
                actor_name=self._resolve_actor(job.uploaded_by),
                description=summary,
            ))

        if event_type:
            entries = [e for e in entries if e.event_type == event_type]

        entries.sort(key=lambda e: e.timestamp, reverse=True)

        return CampaignTimelineResponse(
            campaign_id=campaign_id,
            total_events=len(entries),
            limit=limit,
            offset=offset,
            events=entries[offset: offset + limit],
        )

    def _resolve_actor(self, actor_id: str | None) -> str:
        if not actor_id:
            return "System"
        user = self.campaign_repo.get_user(actor_id)
        return user.full_name if user else "System"

    _SCORING_FIELDS = (
        "weight_deterministic",
        "weight_semantic",
        "weight_ai",
        "semantic_threshold",
        "ai_threshold",
        "deterministic_threshold",
    )

    def update_campaign(
        self,
        campaign_id: UUID,
        request: CampaignUpdateRequest,
        updated_by: str,
    ) -> CampaignResponse:
        try:
            campaign = self.campaign_repo.get_by_id(campaign_id)
            if not campaign:
                raise CampaignException(f"Campaign '{campaign_id}' not found", 404)

            # ── S07-T03: closed campaigns are read-only ──────────────────
            if campaign.status == CampaignStatus.CLOSED:
                # Log the blocked attempt itself (spec requirement), then commit
                # immediately — the raise below triggers this method's own
                # rollback, which would otherwise erase this audit row too.
                self.audit_service.log(
                    actor_id=updated_by,
                    actor_role="HR_ADMIN",
                    action_type=ActionType.CAMPAIGN_EDIT_BLOCKED,
                    entity_type=EntityType.CAMPAIGN,
                    entity_id=campaign.id,
                    campaign_id=campaign.id,
                    details={
                        "title": f"Blocked edit attempt on closed campaign '{campaign.name}'",
                        "attempted_changes": request.model_dump(exclude_unset=True),
                    },
                )
                self.campaign_repo.commit()

                raise CampaignException(
                    "Closed campaigns cannot be edited. Reopen the campaign to make changes.",
                    403,
                )

            changes: dict[str, dict] = {}  # field -> {"before": ..., "after": ...}

            # S01/S02: pause & resume via status change (ACTIVE ⇄ PAUSED)
            paused_now = False
            resumed_now = False
            if request.status is not None and request.status != campaign.status:
                if (campaign.status == CampaignStatus.ACTIVE
                        and request.status == CampaignStatus.PAUSED):
                    changes["status"] = {"before": "ACTIVE", "after": "PAUSED"}
                    campaign.status = CampaignStatus.PAUSED
                    paused_now = True
                elif (campaign.status == CampaignStatus.PAUSED
                        and request.status == CampaignStatus.ACTIVE):
                    changes["status"] = {"before": "PAUSED", "after": "ACTIVE"}
                    campaign.status = CampaignStatus.ACTIVE
                    resumed_now = True
                else:
                    raise CampaignException(
                        f"Unsupported status transition "
                        f"{campaign.status.value} → {request.status.value}.",
                        422,
                    )

            # name / deadline / candidate cap ─────────────────
            if request.name is not None and request.name != campaign.name:
                duplicate = self.campaign_repo.get_by_name(campaign.org_id, request.name)
                if duplicate and duplicate.id != campaign.id:
                    raise CampaignException(
                        f"Campaign name '{request.name}' already exists in this organization",
                        409,
                    )
                changes["name"] = {"before": campaign.name, "after": request.name}
                campaign.name = request.name

            if request.clear_max_candidates:
                if campaign.max_candidates is not None:
                    changes["max_candidates"] = {"before": campaign.max_candidates, "after": None}
                    campaign.max_candidates = None
            elif request.max_candidates is not None and request.max_candidates != campaign.max_candidates:
                current_count = self.campaign_repo.get_candidate_count(campaign.id)
                if request.max_candidates < current_count:
                    raise CampaignException(
                        f"Cannot set candidate cap to {request.max_candidates}: the campaign "
                        f"already has {current_count} candidates.",
                        422,
                    )
                changes["max_candidates"] = {
                    "before": campaign.max_candidates,
                    "after": request.max_candidates,
                }
                campaign.max_candidates = request.max_candidates

            if request.clear_deadline:
                if campaign.deadline is not None:
                    changes["deadline"] = {"before": str(campaign.deadline), "after": None}
                    campaign.deadline = None
            elif request.deadline is not None and request.deadline != campaign.deadline:
                if request.deadline <= datetime.now(timezone.utc):
                    raise CampaignException("Campaign deadline must be a future date", 422)
                changes["deadline"] = {
                    "before": str(campaign.deadline) if campaign.deadline else None,
                    "after": str(request.deadline),
                }
                campaign.deadline = request.deadline

            # ── S03-T01/T03: reassign hiring manager ─────────────────────
            previous_hiring_manager_id = None
            hm_review_pending_count = 0
            if (request.hiring_manager_id is not None
                    and request.hiring_manager_id != campaign.hiring_manager_id):
                new_manager = self.campaign_repo.get_user(request.hiring_manager_id)
                if not new_manager:
                    raise CampaignException(
                        f"User '{request.hiring_manager_id}' not found.", 404,
                    )
                if new_manager.role != LocalUserRole.HIRING_MANAGER:
                    raise CampaignException(
                        f"User '{request.hiring_manager_id}' does not have the "
                        f"HIRING_MANAGER role.", 422,
                    )
                if not new_manager.is_active:
                    raise CampaignException(
                        f"User '{request.hiring_manager_id}' is not an active user.", 422,
                    )

                previous_hiring_manager_id = campaign.hiring_manager_id
                changes["hiring_manager_id"] = {
                    "before": previous_hiring_manager_id,
                    "after": request.hiring_manager_id,
                }
                campaign.hiring_manager_id = request.hiring_manager_id

                # T03: candidates currently in HM_REVIEW may need re-communicating
                # to the incoming manager — computed regardless of who they were
                # visible to before, since no candidate-listing endpoint filters
                # by hiring_manager_id yet (access-revocation is not applicable
                # until that endpoint exists).
                hm_review_pending_count = self.campaign_repo.get_hm_review_count(campaign.id)

            # ── S07-T02: scoring config gate on ACTIVE campaigns ─────────
            scoring_changes = {
                field: getattr(request, field)
                for field in self._SCORING_FIELDS
                if getattr(request, field) is not None
                and Decimal(str(getattr(campaign, field))) != getattr(request, field)
            }

            if scoring_changes:
                if campaign.status == CampaignStatus.ACTIVE and not request.confirm_scoring_change:
                    raise CampaignException(
                        "Changing scoring configuration will only affect candidates submitted "
                        "after this change. Existing candidate scores will not be recalculated. "
                        "Re-submit with confirm_scoring_change=true to proceed.",
                        422,
                    )

                merged_weights = {
                    field: scoring_changes.get(field, Decimal(str(getattr(campaign, field))))
                    for field in ("weight_deterministic", "weight_semantic", "weight_ai")
                }
                if sum(merged_weights.values()) != Decimal("100.00"):
                    raise CampaignException("Scoring weights must sum to 100.00", 422)

                for field, new_value in scoring_changes.items():
                    changes[field] = {
                        "before": str(getattr(campaign, field)),
                        "after": str(new_value),
                    }
                    setattr(campaign, field, float(new_value))

            if not changes:
                raise CampaignException("No changes supplied", 422)

            campaign.updated_at = datetime.now(timezone.utc)
            campaign = self.campaign_repo.update(campaign)

            detail = {"title": f"Campaign '{campaign.name}' updated", "changes": changes}
            if paused_now:
                # S01-T02: soft-cancel QUEUED tasks (RUNNING finish naturally);
                # uploads are blocked immediately by the PAUSED status guard.
                detail["title"] = f"Campaign '{campaign.name}' paused"
                detail["tasks_suspended"] = self.campaign_repo.suspend_queued_tasks(campaign.id)
                detail["in_flight_bulk_jobs"] = self.campaign_repo.count_processing_bulk_jobs(campaign.id)
                action_type = ActionType.CAMPAIGN_PAUSED
            elif resumed_now:
                # S02-T02: re-queue suspended tasks (PAUSED → QUEUED); uploads are
                # re-permitted immediately by the ACTIVE status.
                detail["title"] = f"Campaign '{campaign.name}' resumed"
                detail["tasks_requeued"] = self.campaign_repo.requeue_suspended_tasks(campaign.id)
                detail["resumes_enqueued"] = self.campaign_repo.count_pending_resumes(campaign.id)
                action_type = ActionType.CAMPAIGN_RESUMED
            elif scoring_changes:
                action_type = ActionType.CAMPAIGN_SCORING_CONFIG_CHANGED
            else:
                action_type = ActionType.CAMPAIGN_UPDATED

            self.audit_service.log(
                actor_id=updated_by,
                actor_role="HR_ADMIN",
                action_type=action_type,
                entity_type=EntityType.CAMPAIGN,
                entity_id=campaign.id,
                campaign_id=campaign.id,
                details=detail,
            )

            if previous_hiring_manager_id is not None:
                # S03-T03: dedicated audit entry — always recorded on reassignment,
                # independent of whatever action_type won the main log entry above.
                self.audit_service.log(
                    actor_id=updated_by,
                    actor_role="HR_ADMIN",
                    action_type=ActionType.HIRING_MANAGER_REASSIGNED,
                    entity_type=EntityType.CAMPAIGN,
                    entity_id=campaign.id,
                    campaign_id=campaign.id,
                    details={
                        "title": f"Hiring manager reassigned on campaign '{campaign.name}'",
                        "previous_hiring_manager_id": previous_hiring_manager_id,
                        "new_hiring_manager_id": campaign.hiring_manager_id,
                        "hm_review_pending_count": hm_review_pending_count,
                    },
                )

            self.campaign_repo.commit()

            jd = self.jd_repo.get_by_id(campaign.jd_id)
            candidate_count = self.campaign_repo.get_candidate_count(campaign.id)
            cap_warning_percentage, deadline_warning_days = self._get_warning_thresholds()
            response = CampaignResponse(
                id=campaign.id,
                name=campaign.name,
                status=campaign.status.value,
                jd_title=jd.title if jd else "",
                jd_version=jd.version_number if jd else 0,
                hiring_manager=self._resolve_actor(campaign.hiring_manager_id),
                max_candidates=campaign.max_candidates,
                deadline=campaign.deadline,
                created_at=campaign.created_at,
                candidate_count=candidate_count,
                shortlisted_count=self.campaign_repo.get_shortlisted_count(campaign.id),
                approaching_cap=self._is_approaching_cap(candidate_count, campaign.max_candidates, cap_warning_percentage),
                deadline_soon=self._is_deadline_soon(campaign.deadline, deadline_warning_days),
            )

            if hm_review_pending_count > 0:
                # T03: "a specific warning must alert HR_ADMIN that pending HM
                # review decisions may need to be re-communicated to the new manager"
                response.warning = (
                    f"{hm_review_pending_count} candidate(s) are currently awaiting "
                    f"hiring-manager review. These pending decisions may need to be "
                    f"re-communicated to the newly assigned hiring manager."
                )

            return response

        except Exception:
            self.campaign_repo.rollback()
            raise

    
    def update_campaign_status(self, campaign_id: UUID, status: CampaignStatus) -> HiringCampaign:
        campaign = self.campaign_repo.get_by_id(campaign_id)  # or a small read-only lookup
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")
        if campaign.status == CampaignStatus.CLOSED:
            raise HTTPException(status_code=400, detail="Cannot change status of a closed campaign")
        if campaign.status == CampaignStatus.ACTIVE:
            campaign = self.campaign_repo.update_campaign_status(CampaignStatus.PAUSED, campaign_id)
        elif status == CampaignStatus.PAUSED:
            campaign = self.campaign_repo.update_campaign_status(CampaignStatus.ACTIVE, campaign_id)

        return campaign

    # ── S01 — Pause an Active Campaign ──────────────────────────────────────

    def get_pause_impact_summary(
        self,
        campaign_id: UUID,
    ) -> PauseImpactSummaryResponse:
        """
        S01-T01: read-only data for the pause confirmation dialog. HR_ADMIN only
        (enforced at the route). Only an ACTIVE campaign can be paused.
        """
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException(f"Campaign '{campaign_id}' not found", 404)

        if campaign.status != CampaignStatus.ACTIVE:
            raise CampaignException(
                "Only an active campaign can be paused.", 409
            )

        return PauseImpactSummaryResponse(
            candidate_count=self.campaign_repo.get_candidate_count(campaign_id),
            queued_task_count=self.campaign_repo.count_active_queue_tasks(campaign_id),
            processing_bulk_job_count=self.campaign_repo.count_processing_bulk_jobs(campaign_id),
        )

    # ── S02 — Resume a Paused Campaign ──────────────────────────────────────

    def get_resume_summary(
        self,
        campaign_id: UUID,
    ) -> ResumeSummaryResponse:
        """
        S02-T01: read-only data for the resume confirmation dialog. HR_ADMIN only
        (enforced at the route). Only a PAUSED campaign can be resumed.
        """
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException(f"Campaign '{campaign_id}' not found", 404)

        if campaign.status != CampaignStatus.PAUSED:
            raise CampaignException(
                "Only a paused campaign can be resumed.", 409
            )

        paused = self.campaign_repo.count_paused_tasks(campaign_id)
        pending = self.campaign_repo.count_pending_resumes(campaign_id)
        AVG_SECONDS_PER_ITEM = 45  # rough estimate for the "expected load" hint
        total = paused + pending

        return ResumeSummaryResponse(
            paused_task_count=paused,
            pending_resume_count=pending,
            estimated_processing_seconds=(total * AVG_SECONDS_PER_ITEM) or None,
        )

    def calculate_deterministic_score(
        self,
        jd_id: UUID,
        resume_id: UUID,
        deterministic_threshold: float,
    ) -> tuple[float, bool]:

        mandatory_skills = (
            self.skill_repository.get_mandatory_jd_skills(jd_id)
        )

        candidate_skills = (
            self.skill_repository.get_candidate_normalized_skills(resume_id)
        )

        if not mandatory_skills:
            return 100.0, True

        required_skill_ids = {
            skill.canonical_skill_id
            for skill in mandatory_skills
        }

        candidate_skill_ids = {
            skill.canonical_skill_id
            for skill in candidate_skills
            if skill.canonical_skill_id is not None
        }

        matched_skill_ids = required_skill_ids.intersection(
            candidate_skill_ids
        )

        score = round(
            (len(matched_skill_ids) / len(required_skill_ids)) * 100,
            2,
        )

        passed = score >= float(deterministic_threshold)

        return score, passed

