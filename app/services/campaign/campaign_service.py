from datetime import datetime, timezone
from decimal import Decimal
from urllib import request
from uuid import UUID, uuid4
from datetime import timedelta
from app.middleware.rbac import TokenUser
from app.models.async_tasks import TaskStatus
from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task, DETERMINISTIC_SCORE_TASK_TYPE
from app.tasks.resume_processing_tasks import process_resume_document

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
from app.schemas.campaign.campaign_response import CampaignResponse, CampaignScoringConfigurationResponse, CampaignScoringDefaultsResponse, ScoringLayerExplanationResponse, CopyScoringConfigResponse
from app.schemas.campaign.campaign_schema import CampaignCreateRequest, CampaignUpdateRequest, CampaignScoringUpdateRequest, CopyScoringConfigRequest, PlatformDefaultWeightsUpdateRequest
from app.schemas.campaign.campaign_weight_preset_schema import CampaignWeightPresetCreateRequest, CampaignWeightPresetResponse, CampaignWeightPresetUpdateRequest
from app.services.audit_service import AuditService
from app.schemas.campaign.campaign_pause_schema import PauseImpactSummaryResponse, ResumeSummaryResponse
from app.schemas.campaign.campaign_closure_schema import (
    CampaignCloseRequest,
    CampaignClosureImpactSummaryResponse,
    CampaignClosureResultResponse,
)
from app.schemas.campaign.campaign_reopen_schema import (
    JDReadinessIssue,
    CampaignReopenReadinessResponse,
    CampaignReopenResultResponse,
)
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
from app.schemas.campaign.campaign_comparison_response import (
    CampaignComparisonColumn,
    CampaignComparisonResponse,
    ScoreDistributionResponse,
)
from app.schemas.campaign.campaign_weight_change_report_response import (
    WeightChangeReportRow,
    WeightChangeReportResponse,
)
from app.utils.excel_export import ExcelExport


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

    def _get_review_stall_thresholds(self) -> tuple[int, int]:
        """
        overdue-review / stalled-pipeline thresholds, sourced from
        platform_config (HM_REVIEW_SLA_DAYS / STALE_CAMPAIGN_DAYS).
        """
        configs = self.config_repo.get_configs_by_keys(
            ["HM_REVIEW_SLA_DAYS", "STALE_CAMPAIGN_DAYS"]
        )
        hm_review_sla_days = int(configs.get("HM_REVIEW_SLA_DAYS", "5"))
        stale_campaign_days = int(configs.get("STALE_CAMPAIGN_DAYS", "7"))
        return hm_review_sla_days, stale_campaign_days

    def _validate_scoring_weights(
        self,
        weight_deterministic: Decimal,
        weight_semantic: Decimal,
        weight_ai: Decimal,
    ) -> None:
        """
        shared by every scoring-edit path (update_scoring_configuration
        and update_campaign) so the two can never drift out of sync again — weights
        must sum to 100.00 (also enforced by the DB CHECK constraint
        chk_weights_sum_100; this gives a clean 4xx before that's ever reached),
        and no single layer may fall below MIN_LAYER_WEIGHT, which would bypass
        that layer from the composite score entirely.
        """
        if weight_deterministic + weight_semantic + weight_ai != Decimal("100.00"):
            raise CampaignException("Scoring weights must sum to 100.00", 422)

        min_layer_weight = Decimal(
            self.config_repo.get_configs_by_keys(["MIN_LAYER_WEIGHT"]).get(
                "MIN_LAYER_WEIGHT", "5.00"
            )
        )
        if any(
            w < min_layer_weight
            for w in (weight_deterministic, weight_semantic, weight_ai)
        ):
            raise CampaignException(
                f"Each scoring layer must be at least {min_layer_weight}%.", 400,
            )

    def _already_processed_warning(self, candidate_count: int) -> str | None:
        """
        S02-T03: shared by every scoring-edit path — "a warning must notify
        HR_ADMIN that changes only affect newly submitted candidates."
        """
        if candidate_count <= 0:
            return None
        return (
            f"{candidate_count} candidate(s) were already processed with "
            f"the previous configuration. Their scores will not be "
            f"automatically recalculated."
        )

    def _hiring_manager_name_map(self, campaigns: list[HiringCampaign]) -> dict[str, str]:
        ids = [c.hiring_manager_id for c in campaigns if c.hiring_manager_id]
        return self.campaign_repo.get_hiring_manager_names(ids)

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
                "CAMPAIGN_WEIGHT_DETERMINISTIC",
                "CAMPAIGN_WEIGHT_SEMANTIC",
                "CAMPAIGN_WEIGHT_AI",
                "SEMANTIC_PASS_THRESHOLD",
                "AI_PASS_THRESHOLD",
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
                weight_deterministic=float(
                    configs.get("CAMPAIGN_WEIGHT_DETERMINISTIC", "30.00")
                ),
                weight_semantic=float(
                    configs.get("CAMPAIGN_WEIGHT_SEMANTIC", "40.00")
                ),
                weight_ai=float(
                    configs.get("CAMPAIGN_WEIGHT_AI", "30.00")
                ),
                semantic_threshold=float(
                    configs.get("SEMANTIC_PASS_THRESHOLD", "0.6500")
                ),
                ai_threshold=float(
                    configs.get("AI_PASS_THRESHOLD", "50.00")
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
            field_changes = detail.get("changes", {})

            history_items.append(
                WeightHistoryItemResponse(
                    changed_by=self._resolve_actor(record.actor_id),
                    changed_at=record.created_at,
                    before={field: v.get("before") for field, v in field_changes.items()},
                    after={field: v.get("after") for field, v in field_changes.items()},
                )
            )

        message = None
        if not history_items:
            message = (
                f"No changes — using initial configuration set on "
                f"{campaign.created_at.date().isoformat()}."
            )

        return CampaignWeightHistoryResponse(
            history=history_items,
            message=message,
        )
    def get_all_campaigns(self, user: User, show_closed: bool = False) -> list[CampaignResponse]:
        campaigns = self.campaign_repo.get_all_campaigns(show_closed=show_closed)
        cap_warning_percentage, deadline_warning_days = self._get_warning_thresholds()
        hm_review_sla_days, stale_campaign_days = self._get_review_stall_thresholds()
        hm_names = self._hiring_manager_name_map(campaigns)
        return [
            CampaignResponse(
                id=c.id,
                name=c.name,
                status=c.status.value,
                jd_title=c.job_description.title,
                jd_version=c.job_description.version_number,   # ← matches the actual column name
                hiring_manager=hm_names.get(c.hiring_manager_id, c.hiring_manager_id),
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
                ),
                overdue_review=self.campaign_repo.get_overdue_review_count(c.id, hm_review_sla_days) > 0,
                pipeline_stalled=self.campaign_repo.is_pipeline_stalled(c.id, stale_campaign_days),
            )
            for c in campaigns
        ]

    def get_all_campaigns_for_hrAdmin(self, show_closed: bool = False) -> list[CampaignResponse]:
        # S05-T01: HR_ADMIN must see every campaign in the org, not just the
        # ones they personally created — reuses get_all_campaigns() instead of
        # a separate created_by-scoped repo query.
        campaigns = self.campaign_repo.get_all_campaigns(show_closed=show_closed)
        cap_warning_percentage, deadline_warning_days = self._get_warning_thresholds()
        hm_review_sla_days, stale_campaign_days = self._get_review_stall_thresholds()
        hm_names = self._hiring_manager_name_map(campaigns)
        return [
            CampaignResponse(
                id=c.id,
                name=c.name,
                status=c.status.value,
                jd_title=c.job_description.title,
                jd_version=c.job_description.version_number,
                hiring_manager=hm_names.get(c.hiring_manager_id, c.hiring_manager_id),
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
                ),
                overdue_review=self.campaign_repo.get_overdue_review_count(c.id, hm_review_sla_days) > 0,
                pipeline_stalled=self.campaign_repo.is_pipeline_stalled(c.id, stale_campaign_days),
            )
            for c in campaigns
        ]

    def get_all_campaigns_for_hiring_manager(self, manager_id: UUID, show_closed: bool = False) -> list[CampaignResponse]:
        campaigns = self.campaign_repo.get_all_campaigns_for_hiring_manager(manager_id, show_closed=show_closed)
        cap_warning_percentage, deadline_warning_days = self._get_warning_thresholds()
        hm_review_sla_days, stale_campaign_days = self._get_review_stall_thresholds()
        hm_names = self._hiring_manager_name_map(campaigns)
        return [
            CampaignResponse(
                id=c.id,
                name=c.name,
                status=c.status.value,
                jd_title=c.job_description.title,
                jd_version=c.job_description.version_number,
                hiring_manager=hm_names.get(c.hiring_manager_id, c.hiring_manager_id),
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
                ),
                overdue_review=self.campaign_repo.get_overdue_review_count(c.id, hm_review_sla_days) > 0,
                pipeline_stalled=self.campaign_repo.is_pipeline_stalled(c.id, stale_campaign_days),
            )
            for c in campaigns
        ]


    def search_campaigns(
        self,
        filters: CampaignFilterRequest,
        requesting_user: TokenUser | None = None,
    ) -> list[CampaignResponse]:

        if requesting_user is not None and UserRole.HIRING_MANAGER.value in requesting_user.roles:
            # S05-T01: a HIRING_MANAGER must never see campaigns beyond their
            # own, regardless of what hiring_manager_id filter was requested.
            filters.hiring_manager_id = requesting_user.user_id

        campaigns = self.campaign_repo.search_campaigns(filters)
        cap_warning_percentage, deadline_warning_days = self._get_warning_thresholds()
        hm_review_sla_days, stale_campaign_days = self._get_review_stall_thresholds()
        hm_names = self._hiring_manager_name_map(campaigns)

        return [
            CampaignResponse(
                id=c.id,
                name=c.name,
                status=c.status.value,
                jd_title=c.job_description.title,
                jd_version=c.job_description.version_number,
                hiring_manager=hm_names.get(c.hiring_manager_id, c.hiring_manager_id),
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
                ),
                overdue_review=self.campaign_repo.get_overdue_review_count(c.id, hm_review_sla_days) > 0,
                pipeline_stalled=self.campaign_repo.is_pipeline_stalled(c.id, stale_campaign_days),
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

        self._validate_scoring_weights(
            request.weight_deterministic,
            request.weight_semantic,
            request.weight_ai,
        )

        # T03: capture before/after for every field that actually changed,
        # atomically with the save (audit is written in the same transaction).
        # Uses the same field list as update_campaign()'s scoring path so both
        # edit paths record identical shapes in the Weight Change History.
        changes = {
            field: {
                "before": str(getattr(campaign, field)),
                "after": str(getattr(request, field)),
            }
            for field in self._SCORING_FIELDS
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
            # S01-T03: same action_type update_campaign() uses for scoring edits,
            # so both edit paths land in the same Weight Change History query.
            self.audit_service.log(
                actor_id=updated_by,
                actor_role="HR_ADMIN",
                action_type=ActionType.CAMPAIGN_SCORING_CONFIG_CHANGED,
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
        result.warning = self._already_processed_warning(candidate_count)

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

        self._validate_scoring_weights(
            request.weight_deterministic,
            request.weight_semantic,
            request.weight_ai,
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
            actor_role="HR_ADMIN",
            action_type=ActionType.CAMPAIGN_WEIGHT_PRESET_CREATED.value,
            entity_type=EntityType.CAMPAIGN_WEIGHT_PRESET.value,
            entity_id=preset.id,
            details={
                "title": f"Created campaign weight preset '{preset.name}'"
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
    
    # System presets (Technical/Managerial/Balanced/Entry Level) are hardcoded
    # in get_weight_presets() with these fixed ids — they're never rows in
    # campaign_weight_presets, so update/delete must reject them explicitly
    # instead of relying on a misleading "not found" from a failed lookup.
    _SYSTEM_PRESET_IDS = {
        UUID("00000000-0000-0000-0000-000000000001"),
        UUID("00000000-0000-0000-0000-000000000002"),
        UUID("00000000-0000-0000-0000-000000000003"),
        UUID("00000000-0000-0000-0000-000000000004"),
    }

    def update_weight_preset(
        self,
        preset_id: UUID,
        request: CampaignWeightPresetUpdateRequest,
        org_id: UUID,
        updated_by: str,
    ) -> CampaignWeightPresetResponse:

        if preset_id in self._SYSTEM_PRESET_IDS:
            raise CampaignException(
                "System presets are read-only and cannot be modified.",
                403,
                None,
            )

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

        self._validate_scoring_weights(
            request.weight_deterministic,
            request.weight_semantic,
            request.weight_ai,
        )

        preset.name = request.name.strip()
        preset.description = request.description
        preset.weight_deterministic = request.weight_deterministic
        preset.weight_semantic = request.weight_semantic
        preset.weight_ai = request.weight_ai
        preset.deterministic_threshold = request.deterministic_threshold
        preset.semantic_threshold = request.semantic_threshold
        preset.ai_threshold = request.ai_threshold

        preset = self.preset_repo.update(
            preset
        )

        self.preset_repo.commit()

        self.audit_service.log(
            actor_id=updated_by,
            actor_role="HR_ADMIN",
            action_type=ActionType.CAMPAIGN_WEIGHT_PRESET_UPDATED.value,
            entity_type=EntityType.CAMPAIGN_WEIGHT_PRESET.value,
            entity_id=preset.id,
            details={
                "title": f"Updated preset '{preset.name}'"
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

        if preset_id in self._SYSTEM_PRESET_IDS:
            raise CampaignException(
                "System presets are read-only and cannot be deleted.",
                403,
                None,
            )

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
            actor_role="HR_ADMIN",
            action_type=ActionType.CAMPAIGN_WEIGHT_PRESET_DELETED.value,
            entity_type=EntityType.CAMPAIGN_WEIGHT_PRESET.value,
            entity_id=preset.id,
            details={
                "title": f"Deleted preset '{preset.name}'"
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

    _COMPARISON_FIELDS = (
        "weight_deterministic",
        "weight_semantic",
        "weight_ai",
        "semantic_threshold",
        "ai_threshold",
    )

    def compare_campaigns(self, campaign_ids: list[UUID]) -> CampaignComparisonResponse:
        """
        S04-T01/T03: side-by-side scoring config + score distribution for
        2-4 campaigns, with a per-field "identical across all" flag so the
        frontend doesn't have to recompute the diff itself.
        """
        if not (2 <= len(campaign_ids) <= 4):
            raise CampaignException(
                "Select between 2 and 4 campaigns to compare.", 422,
            )

        columns = []
        for campaign_id in campaign_ids:
            campaign = self.campaign_repo.get_by_id(campaign_id)
            if not campaign:
                raise CampaignException(f"Campaign '{campaign_id}' not found", 404)

            jd = self.jd_repo.get_by_id(campaign.jd_id)
            distribution = self.campaign_repo.get_score_distribution(campaign.id)

            columns.append(
                CampaignComparisonColumn(
                    campaign_id=campaign.id,
                    campaign_name=campaign.name,
                    status=campaign.status.value,
                    jd_title=jd.title if jd else "",
                    weight_deterministic=campaign.weight_deterministic,
                    weight_semantic=campaign.weight_semantic,
                    weight_ai=campaign.weight_ai,
                    semantic_threshold=campaign.semantic_threshold,
                    ai_threshold=campaign.ai_threshold,
                    total_candidates=self.campaign_repo.get_candidate_count(campaign.id),
                    score_distribution=ScoreDistributionResponse(
                        has_processed_candidates=distribution["average"] is not None,
                        message=None if distribution["average"] is not None else "No candidates processed yet.",
                        average_composite_score=distribution["average"],
                        median_composite_score=distribution["median"],
                        highest_composite_score=distribution["highest"],
                        lowest_composite_score=distribution["lowest"],
                        passed_all_layers_count=distribution["passed_all_layers_count"],
                        rejected_deterministic_count=distribution["rejected_deterministic_count"],
                        rejected_semantic_count=distribution["rejected_semantic_count"],
                        rejected_ai_count=distribution["rejected_ai_count"],
                    ),
                )
            )

        consistent_fields = {
            field: len({str(getattr(c, field)) for c in columns}) == 1
            for field in self._COMPARISON_FIELDS
        }

        return CampaignComparisonResponse(campaigns=columns, consistent_fields=consistent_fields)

    _COPYABLE_SCORING_FIELDS = (
        "weight_deterministic",
        "weight_semantic",
        "weight_ai",
        "semantic_threshold",
        "ai_threshold",
    )

    def copy_scoring_configuration(
        self,
        source_campaign_id: UUID,
        request: CopyScoringConfigRequest,
        updated_by: str,
    ) -> CopyScoringConfigResponse:
        """
        S04-T02: copy source's weight_deterministic/semantic/ai and
        semantic_threshold/ai_threshold onto every target campaign (per spec —
        deterministic_threshold is deliberately excluded from the copy scope).
        All-or-nothing: any failing target rolls back every target already
        applied in this call.
        """
        try:
            source = self.campaign_repo.get_by_id(source_campaign_id)
            if not source:
                raise CampaignException(f"Campaign '{source_campaign_id}' not found", 404)

            if source_campaign_id in request.target_campaign_ids:
                raise CampaignException("Source campaign cannot also be a copy target.", 422)

            # Source's weights were already validated when they were saved —
            # this is a defensive re-check before fanning them out to others.
            self._validate_scoring_weights(
                Decimal(str(source.weight_deterministic)),
                Decimal(str(source.weight_semantic)),
                Decimal(str(source.weight_ai)),
            )

            results = []
            for target_id in request.target_campaign_ids:
                target = self.campaign_repo.get_by_id(target_id)
                if not target:
                    raise CampaignException(f"Campaign '{target_id}' not found", 404)

                if target.status == CampaignStatus.CLOSED:
                    raise CampaignException(
                        f"Campaign '{target.name}' is closed and cannot be edited. "
                        f"Reopen the campaign to make changes.",
                        403,
                    )

                changes = {
                    field: {
                        "before": str(getattr(target, field)),
                        "after": str(getattr(source, field)),
                    }
                    for field in self._COPYABLE_SCORING_FIELDS
                    if Decimal(str(getattr(target, field))) != Decimal(str(getattr(source, field)))
                }

                candidate_count = self.campaign_repo.get_candidate_count(target.id)

                for field in self._COPYABLE_SCORING_FIELDS:
                    setattr(target, field, getattr(source, field))
                target.updated_at = datetime.now(timezone.utc)
                target = self.campaign_repo.update(target)

                if changes:
                    self.audit_service.log(
                        actor_id=updated_by,
                        actor_role="HR_ADMIN",
                        action_type=ActionType.CAMPAIGN_SCORING_CONFIG_COPIED.value,
                        entity_type=EntityType.CAMPAIGN.value,
                        entity_id=target.id,
                        campaign_id=target.id,
                        details={
                            "title": f"Scoring configuration copied from '{source.name}' to '{target.name}'",
                            "source_campaign_id": str(source.id),
                            "target_campaign_id": str(target.id),
                            "changes": changes,
                        },
                    )

                result = self.get_scoring_configuration(target.id)
                result.warning = self._already_processed_warning(candidate_count)
                results.append(result)

            self.campaign_repo.commit()

            return CopyScoringConfigResponse(
                source_campaign_id=source_campaign_id,
                results=results,
            )

        except Exception:
            self.campaign_repo.rollback()
            raise

    def reset_scoring_to_defaults(
        self,
        campaign_id: UUID,
        updated_by: str,
    ) -> CampaignScoringConfigurationResponse:
        """
        S05-T01: resets weight_deterministic/semantic/ai and semantic_threshold/
        ai_threshold to the current platform defaults. deterministic_threshold
        is left as-is — the spec's default list doesn't include it. Delegates
        to update_scoring_configuration() so validation, audit logging, and the
        already-processed warning all come from that one implementation rather
        than a second copy of the same rules.
        """
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException(f"Campaign '{campaign_id}' not found", 404)

        configs = self.config_repo.get_configs_by_keys(
            [
                "CAMPAIGN_WEIGHT_DETERMINISTIC",
                "CAMPAIGN_WEIGHT_SEMANTIC",
                "CAMPAIGN_WEIGHT_AI",
                "SEMANTIC_PASS_THRESHOLD",
                "AI_PASS_THRESHOLD",
            ]
        )

        request = CampaignScoringUpdateRequest(
            weight_deterministic=Decimal(configs.get("CAMPAIGN_WEIGHT_DETERMINISTIC", "30.00")),
            weight_semantic=Decimal(configs.get("CAMPAIGN_WEIGHT_SEMANTIC", "40.00")),
            weight_ai=Decimal(configs.get("CAMPAIGN_WEIGHT_AI", "30.00")),
            semantic_threshold=Decimal(configs.get("SEMANTIC_PASS_THRESHOLD", "0.6500")),
            ai_threshold=Decimal(configs.get("AI_PASS_THRESHOLD", "50.00")),
            deterministic_threshold=Decimal(str(campaign.deterministic_threshold)),
        )

        return self.update_scoring_configuration(campaign_id, request, updated_by)

    # Mirrors JDService.EXPORT_AUDIT_ENTITY_ID / BulkUploadService.EXPORT_AUDIT_ENTITY_ID —
    # the fixed sentinel used for audit events with no single owning entity row.
    _PLATFORM_CONFIG_AUDIT_ENTITY_ID = UUID("00000000-0000-0000-0000-000000000000")

    def update_platform_default_weights(
        self,
        request: PlatformDefaultWeightsUpdateRequest,
        updated_by: str,
    ) -> CampaignScoringDefaultsResponse:
        """
        S05-T02: updates the platform_config rows backing get_scoring_configuration's
        "defaults" section and reset_scoring_to_defaults(). Existing campaigns keep
        their own stored weight values untouched — only future defaults/resets change.
        """
        self._validate_scoring_weights(
            request.weight_deterministic,
            request.weight_semantic,
            request.weight_ai,
        )

        updates = {
            "CAMPAIGN_WEIGHT_DETERMINISTIC": str(request.weight_deterministic),
            "CAMPAIGN_WEIGHT_SEMANTIC": str(request.weight_semantic),
            "CAMPAIGN_WEIGHT_AI": str(request.weight_ai),
            "SEMANTIC_PASS_THRESHOLD": str(request.semantic_threshold),
            "AI_PASS_THRESHOLD": str(request.ai_threshold),
        }

        before = self.config_repo.get_configs_by_keys(list(updates.keys()))
        updated = self.config_repo.update_configs(updates, updated_by)
        self.config_repo.commit()

        self.audit_service.log(
            actor_id=updated_by,
            actor_role="HR_ADMIN",
            action_type=ActionType.PLATFORM_CONFIG_UPDATED.value,
            entity_type=EntityType.PLATFORM_CONFIG.value,
            entity_id=self._PLATFORM_CONFIG_AUDIT_ENTITY_ID,
            campaign_id=None,
            details={
                "title": "Platform default scoring weights updated",
                "before": before,
                "after": updated,
            },
        )
        self.audit_service.repository.save()

        return CampaignScoringDefaultsResponse(
            weight_deterministic=float(updated["CAMPAIGN_WEIGHT_DETERMINISTIC"]),
            weight_semantic=float(updated["CAMPAIGN_WEIGHT_SEMANTIC"]),
            weight_ai=float(updated["CAMPAIGN_WEIGHT_AI"]),
            semantic_threshold=float(updated["SEMANTIC_PASS_THRESHOLD"]),
            ai_threshold=float(updated["AI_PASS_THRESHOLD"]),
        )

    def get_weight_change_report(
        self,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        campaign_status: CampaignStatus | None = None,
    ) -> WeightChangeReportResponse:
        """
        S05-T03: one row per CAMPAIGN_SCORING_CONFIG_CHANGED event across every
        campaign in the org. "Candidates processed with this configuration" is
        computed by windowing each campaign's own changes chronologically —
        candidates counted for a given change are those added between it and
        whichever change (if any) superseded it for that same campaign.
        """
        audit_rows = self.audit_service.get_all_scoring_changes(
            date_from=date_from, date_to=date_to, campaign_status=campaign_status,
        )

        by_campaign: dict[UUID, list] = {}
        for log, campaign_name, campaign_status_value, actor_name in audit_rows:
            by_campaign.setdefault(log.campaign_id, []).append(
                (log, campaign_name, campaign_status_value, actor_name)
            )

        rows = []
        for campaign_id, entries in by_campaign.items():
            entries_sorted = sorted(entries, key=lambda e: e[0].created_at)
            for i, (log, campaign_name, campaign_status_value, actor_name) in enumerate(entries_sorted):
                window_end = (
                    entries_sorted[i + 1][0].created_at
                    if i + 1 < len(entries_sorted)
                    else None
                )
                candidate_count = self.campaign_repo.count_candidates_in_window(
                    campaign_id, log.created_at, window_end,
                )

                detail = log.detail or {}
                field_changes = detail.get("changes", {})

                rows.append(
                    WeightChangeReportRow(
                        campaign_id=campaign_id,
                        campaign_name=campaign_name,
                        campaign_status=campaign_status_value.value,
                        change_date=log.created_at,
                        changed_by=actor_name or self._resolve_actor(log.actor_id),
                        previous_weights={f: v.get("before") for f, v in field_changes.items()},
                        new_weights={f: v.get("after") for f, v in field_changes.items()},
                        candidates_processed_with_this_config=candidate_count,
                    )
                )

        rows.sort(key=lambda r: r.change_date, reverse=True)

        return WeightChangeReportResponse(rows=rows, total_count=len(rows))

    def export_weight_change_report_xlsx(
        self,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        campaign_status: CampaignStatus | None = None,
    ):
        report = self.get_weight_change_report(date_from, date_to, campaign_status)
        return ExcelExport.export_weight_change_report(report.rows)

    def _resubmit_paused_tasks(self, campaign_id: UUID) -> int:
        """
        actually resubmits each PAUSED task to the Celery broker,
        reusing its original task_id (so the task's own get_by_task_id()
        lookup finds and reuses this same log row instead of creating a
        duplicate — the same convention resume_processing_tasks.py uses for
        first-submission). Only DETERMINISTIC_SCORE is ever linked to a
        campaign_candidate_id today; anything else is skipped defensively
        rather than guessed at, and left PAUSED for manual follow-up.
        """
        requeued = 0
        for task in self.campaign_repo.get_paused_tasks(campaign_id):
            if task.task_type != DETERMINISTIC_SCORE_TASK_TYPE:
                continue
            calculate_deterministic_score_task.apply_async(
                kwargs={"campaign_candidate_id": str(task.campaign_candidate_id)},
                task_id=str(task.task_id),
            )
            task.status = TaskStatus.QUEUED
            requeued += 1
        self.campaign_repo.db.flush()
        return requeued

    def _enqueue_pending_resume_parses(self, campaign_id: UUID) -> int:
        """
        S02-T02: submits a fresh resume.process_document task for every
        resume that was uploaded while the campaign was paused and never got
        parsed (parse_status = PENDING) — mirroring the exact submission
        pattern resume_intake_service.py uses on a normal upload.
        """
        enqueued = 0
        for resume in self.campaign_repo.get_pending_resumes(campaign_id):
            task_id = uuid4()
            self.campaign_repo.set_resume_task_id(resume, str(task_id))
            process_resume_document.apply_async(
                kwargs={"resume_id": str(resume.id)},
                task_id=str(task_id),
            )
            enqueued += 1
        return enqueued

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
                # S02-T01/T02: same validation update_scoring_configuration() runs —
                # sum must equal 100.00 and no layer may fall below MIN_LAYER_WEIGHT,
                # so this endpoint can't be used to bypass either rule.
                self._validate_scoring_weights(**merged_weights)

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
                detail["tasks_requeued"] = self._resubmit_paused_tasks(campaign.id)
                detail["resumes_enqueued"] = self._enqueue_pending_resume_parses(campaign.id)

                # S02-T03: pause duration, from the matching CAMPAIGN_PAUSED
                # entry's timestamp to now.
                last_pause = self.audit_service.get_latest_entry(
                    campaign.id, ActionType.CAMPAIGN_PAUSED.value,
                )
                if last_pause is not None:
                    paused_at = last_pause.created_at
                    detail["paused_at"] = paused_at.isoformat()
                    detail["pause_duration_seconds"] = (
                        datetime.now(timezone.utc) - paused_at
                    ).total_seconds()

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

            warnings = []
            if hm_review_pending_count > 0:
                # S03-T03: "a specific warning must alert HR_ADMIN that pending HM
                # review decisions may need to be re-communicated to the new manager"
                warnings.append(
                    f"{hm_review_pending_count} candidate(s) are currently awaiting "
                    f"hiring-manager review. These pending decisions may need to be "
                    f"re-communicated to the newly assigned hiring manager."
                )
            if scoring_changes:
                # S02-T03: same warning update_scoring_configuration() shows — must
                # appear regardless of which endpoint made the scoring change.
                scoring_warning = self._already_processed_warning(candidate_count)
                if scoring_warning:
                    warnings.append(scoring_warning)
            if warnings:
                response.warning = " ".join(warnings)

            return response

        except Exception:
            self.campaign_repo.rollback()
            raise

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

    # ── S03 — Close a Campaign Manually ──────────────────────────────────────

    def get_closure_impact_summary(
        self,
        campaign_id: UUID,
    ) -> CampaignClosureImpactSummaryResponse:
        """
        S03-T01: read-only data for the close confirmation dialog. HR_ADMIN
        only (enforced at the route). Only an ACTIVE or PAUSED campaign can
        be closed.
        """
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException(f"Campaign '{campaign_id}' not found", 404)

        if campaign.status not in (CampaignStatus.ACTIVE, CampaignStatus.PAUSED):
            raise CampaignException(
                "Only an active or paused campaign can be closed.", 409,
            )

        return CampaignClosureImpactSummaryResponse(
            candidate_count=self.campaign_repo.get_candidate_count(campaign_id),
            stage_counts=self.campaign_repo.get_stage_counts(campaign_id),
            in_progress_task_count=self.campaign_repo.count_active_queue_tasks(campaign_id),
            pending_human_decision_count=self.campaign_repo.count_pending_human_decision(campaign_id),
            in_progress_bulk_job_count=self.campaign_repo.count_processing_bulk_jobs(campaign_id),
        )

    def close_campaign(
        self,
        campaign_id: UUID,
        request: CampaignCloseRequest,
        updated_by: str,
    ) -> CampaignClosureResultResponse:
        """
        S03-T02/T03: manual, terminal closure — distinct from the automated
        auto-close paths (deadline expiry, candidate cap): those already call
        CampaignRepository.close_campaign() directly. This is the only path
        that kills QUEUED tasks (DEAD, not PAUSED — there's no resume to
        re-queue them later) and cancels in-flight bulk uploads, then builds
        the closure summary and records CAMPAIGN_CLOSED.
        """
        try:
            campaign = self.campaign_repo.get_by_id(campaign_id)
            if not campaign:
                raise CampaignException(f"Campaign '{campaign_id}' not found", 404)

            if campaign.status not in (CampaignStatus.ACTIVE, CampaignStatus.PAUSED):
                raise CampaignException(
                    "Only an active or paused campaign can be closed.", 409,
                )

            tasks_cancelled = self.campaign_repo.kill_queued_tasks(campaign_id)
            bulk_uploads_cancelled = self.campaign_repo.cancel_pending_bulk_jobs(campaign_id)

            self.campaign_repo.close_campaign(campaign)

            stage_counts = self.campaign_repo.get_stage_counts(campaign_id)
            candidate_count = sum(stage_counts.values())

            self.audit_service.log(
                actor_id=updated_by,
                actor_role="HR_ADMIN",
                action_type=ActionType.CAMPAIGN_CLOSED.value,
                entity_type=EntityType.CAMPAIGN.value,
                entity_id=campaign.id,
                campaign_id=campaign.id,
                details={
                    "title": f"Campaign '{campaign.name}' closed",
                    "closure_reason": request.closure_reason.value,
                    "final_pipeline_state": stage_counts,
                    "tasks_cancelled": tasks_cancelled,
                    "bulk_uploads_cancelled": bulk_uploads_cancelled,
                },
            )

            self.campaign_repo.commit()

            return CampaignClosureResultResponse(
                campaign_id=str(campaign.id),
                campaign_name=campaign.name,
                closed_at=campaign.updated_at,
                closure_reason=request.closure_reason,
                candidate_count=candidate_count,
                stage_counts=stage_counts,
                selected_count=stage_counts.get(PipelineStage.SELECTED.value, 0),
                rejected_count=stage_counts.get(PipelineStage.REJECTED.value, 0),
                tasks_cancelled_count=tasks_cancelled,
                bulk_uploads_cancelled_count=bulk_uploads_cancelled,
            )

        except Exception:
            self.campaign_repo.rollback()
            raise

    # ── S04 — Reopen a Closed Campaign ───────────────────────────────────────

    def get_reopen_readiness(
        self,
        campaign_id: UUID,
    ) -> CampaignReopenReadinessResponse:
        """
        S04-T01: read-only readiness check for the reopen confirmation dialog.
        HR_ADMIN only (enforced at the route). Only a CLOSED campaign can be
        reopened.
        """
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException(f"Campaign '{campaign_id}' not found", 404)

        if campaign.status != CampaignStatus.CLOSED:
            raise CampaignException(
                "Only a closed campaign can be reopened.", 409,
            )

        jd = self.jd_repo.get_by_id(campaign.jd_id)
        issues: list[JDReadinessIssue] = []

        if not jd:
            issues.append(JDReadinessIssue(
                code="JD_NOT_FOUND",
                message="The linked job description could not be found.",
            ))
        else:
            if not jd.is_active_version:
                issues.append(JDReadinessIssue(
                    code="JD_NOT_ACTIVE_VERSION",
                    message=f"'{jd.title}' is no longer the active version. Update this campaign to an active JD version before reopening.",
                ))
            if jd.closed_at is not None:
                issues.append(JDReadinessIssue(
                    code="JD_CLOSED",
                    message=f"'{jd.title}' has been closed. Update this campaign to an active, open JD before reopening.",
                ))

            unverified_count = self.campaign_repo.get_mandatory_unverified_skill_count(jd.id)
            if unverified_count > 0:
                issues.append(JDReadinessIssue(
                    code="MANDATORY_SKILLS_UNVERIFIED",
                    message=f"{unverified_count} mandatory skill(s) on '{jd.title}' are still pending verification.",
                ))

            unresolved_count = self.campaign_repo.get_unresolved_unknown_skill_count(jd.id)
            if unresolved_count > 0:
                issues.append(JDReadinessIssue(
                    code="UNRESOLVED_SKILL_EXTRACTION",
                    message=f"{unresolved_count} skill(s) extracted from '{jd.title}' are still unresolved.",
                ))

        return CampaignReopenReadinessResponse(
            is_ready=not issues,
            issues=issues,
            campaign_id=campaign.id,
            campaign_name=campaign.name,
            jd_id=campaign.jd_id,
            jd_title=jd.title if jd else "",
            max_candidates=campaign.max_candidates,
            candidate_count=self.campaign_repo.get_candidate_count(campaign.id),
            deadline=campaign.deadline,
            weight_deterministic=campaign.weight_deterministic,
            weight_semantic=campaign.weight_semantic,
            weight_ai=campaign.weight_ai,
        )

    def reopen_campaign(
        self,
        campaign_id: UUID,
        updated_by: str,
    ) -> CampaignReopenResultResponse:
        """
        S04-T02/T03: reopens a closed campaign back to ACTIVE, re-validating
        readiness first. An already-passed deadline is cleared automatically
        (spec: it must be re-set, not silently kept expired). A cap already
        at/over max_candidates blocks reopen rather than silently allowing an
        over-cap campaign — HR_ADMIN raises/clears it via the existing edit
        endpoint first, reusing that validation instead of duplicating it.
        """
        try:
            readiness = self.get_reopen_readiness(campaign_id)
            if not readiness.is_ready:
                raise CampaignException(
                    "Campaign is not ready to reopen: "
                    + "; ".join(issue.message for issue in readiness.issues),
                    422,
                )

            campaign = self.campaign_repo.get_by_id(campaign_id)

            if campaign.max_candidates is not None:
                candidate_count = self.campaign_repo.get_candidate_count(campaign.id)
                if candidate_count >= campaign.max_candidates:
                    raise CampaignException(
                        f"This campaign already has {candidate_count} candidate(s), at or "
                        f"above its cap of {campaign.max_candidates}. Raise or clear the "
                        f"candidate cap (PATCH the campaign) before reopening.",
                        422,
                    )

            deadline_cleared = False
            if campaign.deadline is not None and campaign.deadline <= datetime.now(timezone.utc):
                campaign.deadline = None
                deadline_cleared = True

            campaign.status = CampaignStatus.ACTIVE
            campaign.updated_at = datetime.now(timezone.utc)
            campaign = self.campaign_repo.update(campaign)

            last_closure = self.audit_service.get_latest_entry(
                campaign.id, ActionType.CAMPAIGN_CLOSED.value,
            )
            original_closure_reason = None
            closed_at = None
            duration_closed_days = None
            if last_closure is not None:
                closed_at = last_closure.created_at
                original_closure_reason = (last_closure.detail or {}).get("closure_reason")
                duration_closed_days = (
                    datetime.now(timezone.utc) - closed_at
                ).total_seconds() / 86400

            self.audit_service.log(
                actor_id=updated_by,
                actor_role="HR_ADMIN",
                action_type=ActionType.CAMPAIGN_REOPENED.value,
                entity_type=EntityType.CAMPAIGN.value,
                entity_id=campaign.id,
                campaign_id=campaign.id,
                details={
                    "title": f"Campaign '{campaign.name}' reopened",
                    "original_closure_reason": original_closure_reason,
                    "closed_at": closed_at.isoformat() if closed_at else None,
                    "duration_closed_days": duration_closed_days,
                    "deadline_cleared": deadline_cleared,
                },
            )

            self.campaign_repo.commit()

            return CampaignReopenResultResponse(
                campaign_id=campaign.id,
                campaign_name=campaign.name,
                status=campaign.status.value,
                reopened_at=campaign.updated_at,
                deadline_cleared=deadline_cleared,
                original_closure_reason=original_closure_reason,
                closed_at=closed_at,
                duration_closed_days=duration_closed_days,
            )

        except Exception:
            self.campaign_repo.rollback()
            raise

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

