import json
import logging
import math
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4
from datetime import timedelta
from app.middleware.rbac import TokenUser
from app.models.async_tasks import TaskStatus
from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task, DETERMINISTIC_SCORE_TASK_TYPE
from app.tasks.resume_processing_tasks import process_resume_document
from app.tasks.embedding_tasks import generate_resume_embedding_task, EMBED_RESUME_TASK_TYPE
from app.tasks.ai_evaluation_tasks import calculate_ai_evaluation_task, AI_EVALUATE_TASK_TYPE
from app.repositories.resume_repository import ResumeRepository

from sqlalchemy.orm import Session

from app.core.encryption_service import EncryptionService
from app.enums.constants import ActionType, COMPOSITE_SCORE_FORMULA_VERSION, EntityType, UserRole
from app.exceptions.campaign_exceptions import CampaignException
from app.models.campaign_weight_preset import CampaignWeightPreset
from app.models.campaigns import CampaignStatus, CampaignWeightConfigurationHistory, HiringCampaign
from app.models.identity import User
from app.models.identity import UserRole as LocalUserRole
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.candidate_repository import CandidateRepository
from app.repositories.encryption_key_repository import EncryptionKeyRepository
from app.repositories.campaign_weight_configuration_history_repository import (
    CampaignWeightConfigurationHistoryRepository,
)
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.jd_repository import JDRepository
from app.repositories.prompt_template_repository import PromptTemplateRepository
from app.services.prompt_template_validation import validate_prompt_template_selection
from app.schemas.campaign.campaign_filter_schema import CampaignFilterRequest
from app.schemas.campaign.campaign_response import CampaignResponse, CampaignScoringConfigurationResponse, CampaignScoringDefaultsResponse, ScoringLayerExplanationResponse, CampaignMinimalResponse, CampaignPageResponse
from app.schemas.campaign.campaign_schema import CampaignCreateRequest, CampaignUpdateRequest, CampaignScoringUpdateRequest, PlatformDefaultWeightsUpdateRequest
from app.schemas.campaign.campaign_weight_preset_schema import CampaignWeightPresetCreateRequest, CampaignWeightPresetResponse, CampaignWeightPresetUpdateRequest
from app.services.audit_service import AuditService
from app.services.campaign.manual_candidate_rescore import enqueue_manual_rescore
from app.services.celery_task_log_service import CeleryTaskLogService
from app.tasks.composite_scoring_tasks import _enqueue_composite_scoring
from app.schemas.campaign.campaign_pause_schema import PauseImpactSummaryResponse, ResumeSummaryResponse
from app.schemas.campaign.campaign_closure_schema import (CampaignCloseRequest,
    CampaignClosureImpactSummaryResponse,
    CampaignClosureResultResponse,
)
from app.schemas.campaign.campaign_reopen_schema import (JDReadinessIssue,
    CampaignReopenReadinessResponse,
    CampaignReopenResultResponse,
)
from app.schemas.campaign.campaign_response import (CampaignWeightHistoryResponse,
    WeightHistoryItemResponse,
)
from app.core.config import settings
from app.core.cache_keys import (
    campaign_key,
    campaign_list_key,
    campaign_list_prefix,
    campaign_platform_defaults_key,
    campaign_scoring_key,
    campaign_weight_presets_key,
)
from app.services.cache_service import CacheService
from app.repositories.campaign_weight_preset_repository import (CampaignWeightPresetRepository,
)
from app.schemas.campaign.campaign_detail_response import (CampaignDetailResponse,
    CampaignInfoSection,
    JDConfigSection,
    ScoringConfigSection,
    PipelineLimitsSection,
    HiringManagerSection,
)
from app.models.pipeline import CompositeScoreTriggerSource, DecisionSource, PipelineStage, TransitionSource
from app.schemas.campaign.campaign_monitoring_schema import (StalledCandidateItem,
    StalledCandidatesResponse,
    StageOverrideRequest,
    FlagReviewRequest,
    EscalateStallRequest,
    StalledActionResponse,
    RejectionReasonItem,
    MissingSkillItem,
    RejectionRecommendation,
    RejectionAnalyticsResponse,
)
from app.schemas.campaign.pipeline_summary_response import PipelineSummaryResponse, StageStat
from app.schemas.campaign.campaign_processing_status_response import (ProcessingStatusSummaryResponse,
    DeadLetterQueueEntryResponse,
    DeadLetterQueuePageResponse,
)
from app.schemas.campaign.campaign_processing_queue_response import (TaskTypeBreakdownResponse,
    CircuitBreakerSummaryResponse,
    EstimatedCompletionResponse,
    ProcessingQueueResponse,
    DLQReplayResultItem,
    DLQReplayResponse,
)
from app.repositories.circuit_breaker_repository import CircuitBreakerRepository
from app.repositories.dead_letter_queue_repository import DeadLetterQueueRepository
from app.repositories.interview_schedule_repository import InterviewScheduleRepository
from app.schemas.campaign.campaign_timeline_response import CampaignTimelineResponse, TimelineEntry

logger = logging.getLogger(__name__)

# M10-E01 Design Decision 9: weight-field names whose change (as opposed to
# a threshold-only change) must trigger a composite-score recalculation.
_WEIGHT_FIELDS = {"weight_deterministic", "weight_semantic", "weight_ai"}

# M12 cascading-cancellation follow-up: same constant, same reasoning as
# StageTransitionService's own copy (see that file for the full
# rationale) - duplicated, not imported, since this is the third of the
# 3 places a candidate can leave INTERVIEW from (override_candidate_stage
# can reach SELECTED/SHORTLISTED via an explicit target_stage even though
# neither is this stage's own _STAGE_OVERRIDE_NEXT default).
_INTERVIEW_EXIT_CASCADE_STAGES = frozenset(
    {PipelineStage.SELECTED, PipelineStage.REJECTED, PipelineStage.SHORTLISTED},
)


class CampaignService:

    def __init__(self,
        campaign_repo: CampaignRepository,
        jd_repo: JDRepository,
        audit_service: AuditService,
        config_repo: ConfigRepository,
        preset_repo: CampaignWeightPresetRepository,
        db: Session,
        circuit_breaker_repo: CircuitBreakerRepository | None = None,
        dead_letter_queue_repo: DeadLetterQueueRepository | None = None,
        prompt_template_repo: PromptTemplateRepository | None = None,
        campaign_weight_configuration_history_repo: CampaignWeightConfigurationHistoryRepository | None = None,
        resume_repo: ResumeRepository | None = None,
        cache_service: CacheService | None = None,
        interview_schedule_repo: InterviewScheduleRepository | None = None,
    ):
        self.cache_service = cache_service
        self.campaign_repo = campaign_repo
        self.jd_repo = jd_repo
        self.audit_service = audit_service
        self.config_repo = config_repo
        self.preset_repo = preset_repo
        self.db = db
        # processing-queue / DLQ-replay / prompt-template deps — each defaulted
        # from the same session so pre-existing constructor call sites keep
        # working unchanged.
        self.circuit_breaker_repo = circuit_breaker_repo or CircuitBreakerRepository(db)
        self.dead_letter_queue_repo = dead_letter_queue_repo or DeadLetterQueueRepository(db)
        # EMBED_RESUME DLQ-replay pre-checks (resume exists / has parsed_json
        # / doesn't already have an embedding) - same defaulted-from-db
        # convention as the repos above.
        self.resume_repo = resume_repo or ResumeRepository(db)
        # M10-E02 — same defaulted-from-db convention as the two repos above,
        # so the existing get_campaign_service DI wiring needs no change.
        self.campaign_weight_configuration_history_repo = (
            campaign_weight_configuration_history_repo or CampaignWeightConfigurationHistoryRepository(db)
        )
        self.prompt_template_repo = prompt_template_repo or PromptTemplateRepository(db)
        # M12 gap fix — override_candidate_stage is a third path (besides
        # StageTransitionService.transition() and
        # PipelineTransitionService.transition_stage()) that can reach
        # to_stage=INTERVIEW, found live when a candidate moved there via
        # this override had no interview_schedules row at all. Same
        # defaulted-from-db convention as the repos above, so
        # get_campaign_service's existing DI wiring needs no change.
        self.interview_schedule_repo = interview_schedule_repo or InterviewScheduleRepository(db)

    def _get_warning_thresholds(self) -> tuple[float, int]:
        """
        cap/deadline warning thresholds, sourced from platform_config
        (CAP_WARNING_PERCENTAGE / DEADLINE_WARNING_DAYS) with the previous
        hardcoded values (80%, 3 days) as fallback if the keys aren't seeded.
        """
        configs = self.config_repo.get_configs_by_keys(["CAP_WARNING_PERCENTAGE", "DEADLINE_WARNING_DAYS"]
        )
        cap_warning_percentage = float(configs.get("CAP_WARNING_PERCENTAGE", "80.00"))
        deadline_warning_days = int(configs.get("DEADLINE_WARNING_DAYS", "3"))
        return cap_warning_percentage, deadline_warning_days

    def _get_review_stall_thresholds(self) -> tuple[int, int]:
        """
        overdue-review / stalled-pipeline thresholds, sourced from
        platform_config (HM_REVIEW_SLA_DAYS / STALE_CAMPAIGN_DAYS).
        """
        configs = self.config_repo.get_configs_by_keys(["HM_REVIEW_SLA_DAYS", "STALE_CAMPAIGN_DAYS"]
        )
        hm_review_sla_days = int(configs.get("HM_REVIEW_SLA_DAYS", "5"))
        stale_campaign_days = int(configs.get("STALE_CAMPAIGN_DAYS", "7"))
        return hm_review_sla_days, stale_campaign_days

    def _validate_scoring_weights(self,
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

        M10-E02: also defensively re-validates each individual weight is
        within [0, 100] - CampaignScoringUpdateRequest already enforces
        this at the schema layer, but update_campaign's PATCH body
        (CampaignUpdateRequest) did not until this same epic added matching
        Field(ge=0, le=100) constraints there too; this service-level check
        is the second, independent line of defense neither request schema
        can be relied on alone (M10-E02's own "no partial writes" mandate).
        A pair like weight_deterministic=-50/weight_semantic=200/weight_ai=-50
        sums to 100.00 but must still be rejected.
        """
        if any(w < 0 or w > 100 for w in (weight_deterministic, weight_semantic, weight_ai)):
            raise CampaignException("Each scoring weight must be between 0 and 100.", 422)

        if weight_deterministic + weight_semantic + weight_ai != Decimal("100.00"):
            raise CampaignException("Scoring weights must sum to 100.00", 422)

        min_layer_weight = Decimal(self.config_repo.get_configs_by_keys(["MIN_LAYER_WEIGHT"]).get("MIN_LAYER_WEIGHT", "5.00"
            )
        )
        if any(w < min_layer_weight
            for w in (weight_deterministic, weight_semantic, weight_ai)
        ):
            raise CampaignException(f"Each scoring layer must be at least {min_layer_weight}%.", 400,
            )

    def _enqueue_composite_recalculation_for_campaign(self, campaign_id: UUID) -> None:
        """
        M10-E01 Design Decision 9: a campaign's scoring weights changing
        recalculates ONLY composite_score for every existing candidate in
        that campaign - deterministic_score/semantic_score/effective_ai_score
        are never recomputed, since none of those layers depend on
        weight_deterministic/weight_semantic/weight_ai. Shared by both
        scoring-edit paths (update_scoring_configuration and
        update_campaign) so they can never drift out of sync, same
        reasoning as _validate_scoring_weights above. Reuses the exact same
        enqueue/idempotency helper every other composite-score trigger uses
        (app.tasks.composite_scoring_tasks._enqueue_composite_scoring) -
        never a second/independent implementation. Best-effort, called only
        after the weight-change transaction has already committed - a
        failure here must never undo that already-successful change, same
        convention as CampaignCandidateService._queue_post_override_evaluation.
        """
        try:
            campaign_candidate_repo = CampaignCandidateRepository(self.db)
            task_log_service = CeleryTaskLogService(CeleryTaskLogRepository(self.db))
            for campaign_candidate_id in campaign_candidate_repo.get_ids_by_campaign(campaign_id):
                _enqueue_composite_scoring(
                    campaign_candidate_id, task_log_service, CompositeScoreTriggerSource.CAMPAIGN_WEIGHT_CHANGE,
                )
        except Exception:
            logger.exception(
                "Failed to enqueue composite-score recalculation for campaign_id=%s", campaign_id,
            )

    def _record_weight_configuration_change(
        self,
        campaign: HiringCampaign,
        old_weights: dict[str, Decimal],
        changed_by: str,
    ) -> None:
        """
        M10-E02 Story 2: inserts one immutable campaign_weight_configuration_history
        row and writes one CAMPAIGN_WEIGHT_CONFIGURATION_CHANGED audit entry,
        capturing the before (`old_weights`, read by the caller BEFORE
        mutating `campaign`) and after (`campaign`'s current, already-updated
        weight_deterministic/semantic/ai) values. Shared by both scoring-edit
        paths (update_scoring_configuration and update_campaign) so the two
        can never drift out of sync, same reasoning as
        _validate_scoring_weights/_enqueue_composite_recalculation_for_campaign
        above.

        Callers are expected to invoke this ONLY when the weight fields
        actually changed (gated the same way _enqueue_composite_recalculation_for_campaign
        already is) - a no-op resubmission of identical weights must never
        reach this method, so no additional no-op check is duplicated here.
        Flushes only (via the repository/audit_service) - does not commit;
        that remains the caller's responsibility, so this row is rolled
        back together with the campaign update and its own audit entry as
        one atomic unit on any downstream failure.
        """
        self.campaign_weight_configuration_history_repo.create(CampaignWeightConfigurationHistory(
            campaign_id=campaign.id,
            old_weight_deterministic=old_weights["weight_deterministic"],
            old_weight_semantic=old_weights["weight_semantic"],
            old_weight_ai=old_weights["weight_ai"],
            new_weight_deterministic=campaign.weight_deterministic,
            new_weight_semantic=campaign.weight_semantic,
            new_weight_ai=campaign.weight_ai,
            changed_by=changed_by,
            formula_version=COMPOSITE_SCORE_FORMULA_VERSION,
        ))

        self.audit_service.log(
            actor_id=changed_by,
            actor_role="HR_ADMIN",
            action_type=ActionType.CAMPAIGN_WEIGHT_CONFIGURATION_CHANGED,
            entity_type=EntityType.CAMPAIGN,
            entity_id=campaign.id,
            campaign_id=campaign.id,
            details={
                "title": f"Campaign '{campaign.name}' weight configuration changed",
                "old_weights": {
                    "weight_deterministic": str(old_weights["weight_deterministic"]),
                    "weight_semantic": str(old_weights["weight_semantic"]),
                    "weight_ai": str(old_weights["weight_ai"]),
                },
                "new_weights": {
                    "weight_deterministic": str(campaign.weight_deterministic),
                    "weight_semantic": str(campaign.weight_semantic),
                    "weight_ai": str(campaign.weight_ai),
                },
                "changed_by": changed_by,
                "formula_version": COMPOSITE_SCORE_FORMULA_VERSION,
            },
        )

    def _already_processed_warning(self, candidate_count: int) -> str | None:
        """
        shared by every scoring-edit path — "a warning must notify
        HR_ADMIN that changes only affect newly submitted candidates."
        """
        if candidate_count <= 0:
            return None
        return (f"{candidate_count} candidate(s) were already processed with "
            f"the previous configuration. Their scores will not be "
            f"automatically recalculated."
        )

    def _prompt_name_map(self, campaigns: list[HiringCampaign]) -> dict[UUID, str]:
        return self.prompt_template_repo.get_names_by_ids(
            [c.prompt_template_id for c in campaigns]
        )

    def _hiring_manager_name_map(self, campaigns: list[HiringCampaign]) -> dict[str, str]:
        ids = [c.hiring_manager_id for c in campaigns if c.hiring_manager_id]
        return self.campaign_repo.get_hiring_manager_names(ids)

    def _campaign_aggregate_maps(
        self,
        campaigns: list[HiringCampaign],
        hm_review_sla_days: int,
        stale_campaign_days: int,
    ) -> dict[str, dict]:
        """
        Batches the 5 per-campaign aggregate lookups (candidate/shortlisted/
        selected/overdue-review counts + pipeline-stalled flag) that every
        campaign list endpoint annotates each row with - same
        batch-once-before-the-loop pattern as _prompt_name_map/
        _hiring_manager_name_map above, replacing 5 queries per campaign
        row with 5 queries total for the whole page.
        """
        campaign_ids = [c.id for c in campaigns]
        return {
            "candidate_counts": self.campaign_repo.get_candidate_counts(campaign_ids),
            "shortlisted_counts": self.campaign_repo.get_shortlisted_counts(campaign_ids),
            "selected_counts": self.campaign_repo.get_selected_counts(campaign_ids),
            "overdue_review_counts": self.campaign_repo.get_overdue_review_counts(
                campaign_ids, hm_review_sla_days,
            ),
            "pipeline_stalled_map": self.campaign_repo.get_pipeline_stalled_map(
                campaign_ids, stale_campaign_days,
            ),
        }

    def _close_if_all_positions_filled(self,
        campaign_id: UUID,
        actor_id: str,
        actor_role: str | None = None,
    ) -> bool:
        """
        max_candidates is an openings count consumed at SELECTED, so the
        campaign auto-closes once every position is filled. Mirrors
        PipelineTransitionService._close_if_all_positions_filled — both stage
        writers must enforce this or the override path bypasses the cap.
        Does not commit; the caller does.
        """
        campaign = self.campaign_repo.get_by_id_for_update(campaign_id)
        if campaign is None or not campaign.max_candidates:
            return False
        if campaign.status == CampaignStatus.CLOSED:
            return False
        if self.campaign_repo.get_selected_count(campaign.id) < campaign.max_candidates:
            return False

        campaign.status = CampaignStatus.CLOSED
        campaign.updated_at = datetime.now(timezone.utc)
        self.campaign_repo.update(campaign)

        self.audit_service.log(actor_id=actor_id,
            actor_role=actor_role,
            action_type=ActionType.CAMPAIGN_AUTO_CLOSED,
            entity_type=EntityType.CAMPAIGN,
            entity_id=campaign.id,
            campaign_id=campaign.id,
            details={
                "title": f"Campaign '{campaign.name}' auto-closed",
                "reason": "ALL_POSITIONS_FILLED",
                "max_candidates": campaign.max_candidates,
                "selected_count": campaign.max_candidates,
            },
        )
        return True

    def _is_approaching_cap(self,
        selected_count: int,
        max_candidates: int | None,
        warning_percentage: float = 80.0,
    ) -> bool:
        """
        True once warning_percentage of the campaign's openings are filled.

        max_candidates is an openings count, so this must be passed the number
        of SELECTED candidates - not total intake. Passing the candidate count
        would flag every campaign that simply received a lot of resumes.
        """
        if not max_candidates:
            return False

        return selected_count >= (max_candidates * (warning_percentage / 100))


    def _is_deadline_soon(self,
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

    def  create_campaign(self,
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
                raise CampaignException("Invalid job description: Job description not found",
                    422
                )

            if not jd.is_active_version:
                raise CampaignException("Invalid job description: Job description is not the active version",
                    422
                )

            if jd.closed_at is not None:
                raise CampaignException("Invalid job description: Job description is closed",
                    422
                )


            existing_campaign = self.campaign_repo.get_by_name(org_id, request.name)
            if existing_campaign:
                raise CampaignException(f"Campaign name '{existing_campaign.name}' already exists in this organization",
                    409)


            if request.deadline:
                if request.deadline <= datetime.now(timezone.utc):
                    raise CampaignException("Campaign deadline must be a future date", 422)

            selected_prompt = validate_prompt_template_selection(
                request.prompt_template_id,
                expected_task_type="RESUME_PARSE",
                repository=self.prompt_template_repo,
                exception_factory=lambda msg: CampaignException(msg, 422),
            )

            selected_ai_evaluate_prompt = validate_prompt_template_selection(
                request.ai_evaluate_prompt_id,
                expected_task_type="AI_EVALUATE",
                repository=self.prompt_template_repo,
                exception_factory=lambda msg: CampaignException(msg, 422),
            )

            campaign = HiringCampaign(org_id=org_id,
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
                prompt_template_id=selected_prompt.id,
                ai_evaluate_prompt_id=selected_ai_evaluate_prompt.id,
                hiring_manager_id=request.hiring_manager_id,
                recruiter_id=request.recruiter_id,
                created_by=created_by,
            )

            
            campaign = self.campaign_repo.create_campaign(campaign)

            # Same transaction as the campaign itself: rolled back together on
            # failure. campaign_id is what the activity timeline filters on.
            self.audit_service.log(actor_id=created_by,
                actor_role="HR_ADMIN",
                action_type=ActionType.CAMPAIGN_CREATED,
                entity_type=EntityType.CAMPAIGN,
                entity_id=campaign.id,
                campaign_id=campaign.id,
                details={
                    "title": f"Campaign '{campaign.name}' created",
                    "jd_id": str(campaign.jd_id),
                    "previous_prompt_template_id": None,
                    "new_prompt_template_id": str(campaign.prompt_template_id),
                    "previous_ai_evaluate_prompt_id": None,
                    "new_ai_evaluate_prompt_id": str(campaign.ai_evaluate_prompt_id),
                },
            )

            self.campaign_repo.commit()
            if self.cache_service:
                self.cache_service.delete_by_prefix(campaign_list_prefix())


            hiring_manager_name = request.hiring_manager_id
            # if campaign.hiring_manager_id:
            #     hiring_manager = self.db.query(User).filter(User.id == campaign.hiring_manager_id).first()
            #     if hiring_manager:
            #         hiring_manager_name = hiring_manager.full_name

            cap_warning_percentage, deadline_warning_days = self._get_warning_thresholds()
            candidate_count = self.campaign_repo.get_candidate_count(campaign.id)

            return CampaignResponse(id=campaign.id,
                name=campaign.name,
                status=campaign.status.value,
                jd_title=jd.title,
                jd_version=jd.version_number,
                hiring_manager=hiring_manager_name,
                max_candidates=campaign.max_candidates,
                deadline=campaign.deadline,
                created_at=campaign.created_at,
                prompt_template_id=campaign.prompt_template_id,
                prompt_name=selected_prompt.name,
                ai_evaluate_prompt_id=campaign.ai_evaluate_prompt_id,
                ai_evaluate_prompt_name=selected_ai_evaluate_prompt.name,
                candidate_count=candidate_count,
                shortlisted_count=self.campaign_repo.get_shortlisted_count(campaign.id),
                approaching_cap=self._is_approaching_cap(self.campaign_repo.get_selected_count(campaign.id),
                    campaign.max_candidates,
                    cap_warning_percentage,
                ),
                deadline_soon=self._is_deadline_soon(campaign.deadline,
                    deadline_warning_days,
                )
            )

        except Exception:
            self.campaign_repo.rollback()
            raise

    def _invalidate_campaign_caches(self, campaign_id: UUID, org_id: UUID | None = None) -> None:
        if not self.cache_service:
            return
        self.cache_service.delete(campaign_key(campaign_id), campaign_scoring_key(campaign_id))
        self.cache_service.delete_by_prefix(campaign_list_prefix())
        if org_id is not None:
            self.cache_service.delete(campaign_weight_presets_key(org_id))

    def get_campaign_by_id(self, campaign_id: UUID) -> CampaignResponse:
        if not self.cache_service:
            return self._load_campaign_by_id(campaign_id)

        raw = self.cache_service.get_or_set(
            campaign_key(campaign_id),
            loader=lambda: self._load_campaign_by_id(campaign_id).model_dump_json(),
            ttl=settings.cache_campaign_ttl_seconds,
        )
        return CampaignResponse.model_validate_json(raw)

    def _load_campaign_by_id(self, campaign_id: UUID) -> CampaignResponse:

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
                self.campaign_repo.get_selected_count(campaign.id),
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
        if not self.cache_service:
            return self._load_scoring_configuration(campaign_id)

        raw = self.cache_service.get_or_set(
            campaign_scoring_key(campaign_id),
            loader=lambda: self._load_scoring_configuration(campaign_id).model_dump_json(),
            ttl=settings.cache_campaign_ttl_seconds,
        )
        return CampaignScoringConfigurationResponse.model_validate_json(raw)

    def _load_scoring_configuration(self, campaign_id: UUID) -> CampaignScoringConfigurationResponse:

        campaign = self.campaign_repo.get_by_id(campaign_id)

        if not campaign:
            raise CampaignException(f"Campaign with ID '{campaign_id}' not found",
                404,
                None,
            )

        configs = self.config_repo.get_configs_by_keys([
                "CAMPAIGN_WEIGHT_DETERMINISTIC",
                "CAMPAIGN_WEIGHT_SEMANTIC",
                "CAMPAIGN_WEIGHT_AI",
                "SEMANTIC_PASS_THRESHOLD",
                "AI_PASS_THRESHOLD",
            ]
        )
        formula = "((det × w_det) + (sem × 100 × w_sem) + (eff_ai × w_ai)) / 100"
        layers = [
            ScoringLayerExplanationResponse(layer="Deterministic",
                weight=campaign.weight_deterministic,
                threshold=campaign.deterministic_threshold,
                description="Mandatory skill, experience and education validation.",
                ),
                ScoringLayerExplanationResponse(layer="Semantic",
                    weight=campaign.weight_semantic,
                    threshold=campaign.semantic_threshold,
                    description="Contextual similarity between Job Description and Resume.",
                ),
                ScoringLayerExplanationResponse(layer="AI Evaluation",
                    weight=campaign.weight_ai,
                    threshold=campaign.ai_threshold,
                    description="LLM generated ATS evaluation score.",
                ),
            ]
        

        total_weight = (campaign.weight_deterministic
            + campaign.weight_semantic
            + campaign.weight_ai
        )

        return CampaignScoringConfigurationResponse(weight_deterministic=campaign.weight_deterministic,
            weight_semantic=campaign.weight_semantic,
            weight_ai=campaign.weight_ai,
            semantic_threshold=campaign.semantic_threshold,
            ai_threshold=campaign.ai_threshold,
            deterministic_threshold=campaign.deterministic_threshold,
            total_weight=total_weight,
            formula=formula,
            layers=layers,
            defaults=CampaignScoringDefaultsResponse(weight_deterministic=float(configs.get("CAMPAIGN_WEIGHT_DETERMINISTIC", "30.00")
                ),
                weight_semantic=float(configs.get("CAMPAIGN_WEIGHT_SEMANTIC", "40.00")
                ),
                weight_ai=float(configs.get("CAMPAIGN_WEIGHT_AI", "30.00")
                ),
                semantic_threshold=float(configs.get("SEMANTIC_PASS_THRESHOLD", "0.6500")
                ),
                ai_threshold=float(configs.get("AI_PASS_THRESHOLD", "50.00")
                ),
            ),
        )
    
    def get_platform_scoring_defaults(self) -> CampaignScoringDefaultsResponse:
        if not self.cache_service:
            return self._load_platform_scoring_defaults()

        raw = self.cache_service.get_or_set(
            campaign_platform_defaults_key(),
            loader=lambda: self._load_platform_scoring_defaults().model_dump_json(),
            ttl=settings.cache_campaign_ttl_seconds,
        )
        return CampaignScoringDefaultsResponse.model_validate_json(raw)

    def _load_platform_scoring_defaults(self) -> CampaignScoringDefaultsResponse:
        """
        Org-wide default weights/thresholds from platform_config — used by
        the new-campaign form to prefill and by Reset to Defaults previews.
        No campaign context required.
        """
        configs = self.config_repo.get_configs_by_keys([
                "CAMPAIGN_WEIGHT_DETERMINISTIC",
                "CAMPAIGN_WEIGHT_SEMANTIC",
                "CAMPAIGN_WEIGHT_AI",
                "SEMANTIC_PASS_THRESHOLD",
                "AI_PASS_THRESHOLD",
            ]
        )
        return CampaignScoringDefaultsResponse(weight_deterministic=float(configs.get("CAMPAIGN_WEIGHT_DETERMINISTIC", "30.00")),
            weight_semantic=float(configs.get("CAMPAIGN_WEIGHT_SEMANTIC", "40.00")),
            weight_ai=float(configs.get("CAMPAIGN_WEIGHT_AI", "30.00")),
            semantic_threshold=float(configs.get("SEMANTIC_PASS_THRESHOLD", "0.6500")),
            ai_threshold=float(configs.get("AI_PASS_THRESHOLD", "50.00")),
        )

    def get_scoring_history(self,
        campaign_id: UUID,
    ) -> CampaignWeightHistoryResponse:

        campaign = self.campaign_repo.get_by_id(campaign_id)

        if not campaign:
            raise CampaignException(f"Campaign with ID '{campaign_id}' not found",
                404,
                None,
            )

        history = self.audit_service.get_campaign_scoring_history(campaign_id
        )

        history_items = []

        for record in history:

            detail = record.detail or {}
            field_changes = detail.get("changes", {})

            history_items.append(WeightHistoryItemResponse(changed_by=self._resolve_actor(record.actor_id),
                    changed_at=record.created_at,
                    before={field: v.get("before") for field, v in field_changes.items()},
                    after={field: v.get("after") for field, v in field_changes.items()},
                )
            )

        message = None
        if not history_items:
            message = (f"No changes — using initial configuration set on "
                f"{campaign.created_at.date().isoformat()}."
            )

        return CampaignWeightHistoryResponse(history=history_items,
            message=message,
        )
    def get_active_campaigns_minimal(self) -> list[CampaignMinimalResponse]:
        """id + name only, for dropdowns/pickers — HR_ADMIN/RECRUITER (enforced at the route)."""
        rows = self.campaign_repo.get_active_campaigns_minimal()
        return [CampaignMinimalResponse(id=row.id, name=row.name) for row in rows]

    def get_all_campaigns(self, user: User, show_closed: bool = False) -> list[CampaignResponse]:
        if not self.cache_service:
            return self._load_all_campaigns(show_closed)
        raw = self.cache_service.get_or_set(
            campaign_list_key({"kind": "all", "show_closed": show_closed}),
            loader=lambda: json.dumps(
                [c.model_dump(mode="json") for c in self._load_all_campaigns(show_closed)]
            ),
            ttl=settings.cache_campaign_list_ttl_seconds,
        )
        return [CampaignResponse.model_validate(item) for item in json.loads(raw)]

    def _load_all_campaigns(self, show_closed: bool) -> list[CampaignResponse]:
        campaigns = self.campaign_repo.get_all_campaigns(show_closed=show_closed)
        cap_warning_percentage, deadline_warning_days = self._get_warning_thresholds()
        hm_review_sla_days, stale_campaign_days = self._get_review_stall_thresholds()
        hm_names = self._hiring_manager_name_map(campaigns)
        prompt_names = self._prompt_name_map(campaigns)
        aggregates = self._campaign_aggregate_maps(campaigns, hm_review_sla_days, stale_campaign_days)
        return [
            CampaignResponse(id=c.id,
                name=c.name,
                status=c.status.value,
                jd_title=c.job_description.title,
                jd_version=c.job_description.version_number,   # ← matches the actual column name
                hiring_manager=hm_names.get(c.hiring_manager_id, c.hiring_manager_id),
                max_candidates=c.max_candidates,
                deadline=c.deadline,
                created_at=c.created_at,
                prompt_template_id=c.prompt_template_id,
                prompt_name=prompt_names.get(c.prompt_template_id),
                candidate_count=aggregates["candidate_counts"].get(c.id, 0),
                shortlisted_count=aggregates["shortlisted_counts"].get(c.id, 0),
                approaching_cap=self._is_approaching_cap(aggregates["selected_counts"].get(c.id, 0),
                    c.max_candidates,
                    cap_warning_percentage,
                ),
                deadline_soon=self._is_deadline_soon(c.deadline,
                    deadline_warning_days,
                ),
                overdue_review=aggregates["overdue_review_counts"].get(c.id, 0) > 0,
                pipeline_stalled=aggregates["pipeline_stalled_map"].get(c.id, False),
            )
            for c in campaigns
        ]

    def get_all_campaigns_for_hrAdmin(
        self,
        created_by: str,
        show_closed: bool = False,
        search: str | None = None,
        status: CampaignStatus | None = None,
        page: int = 1,
        page_size: int = 6,
    ) -> CampaignPageResponse:
        if not self.cache_service:
            return self._load_all_campaigns_for_hr_admin(created_by, show_closed, search, status, page, page_size)
        raw = self.cache_service.get_or_set(
            campaign_list_key({
                "kind": "hr-admin", "created_by": created_by, "show_closed": show_closed,
                "search": search, "status": status.value if status else None,
                "page": page, "page_size": page_size,
            }),
            loader=lambda: self._load_all_campaigns_for_hr_admin(
                created_by, show_closed, search, status, page, page_size,
            ).model_dump_json(),
            ttl=settings.cache_campaign_list_ttl_seconds,
        )
        return CampaignPageResponse.model_validate_json(raw)

    def _load_all_campaigns_for_hr_admin(
        self,
        created_by: str,
        show_closed: bool,
        search: str | None,
        status: CampaignStatus | None,
        page: int,
        page_size: int,
    ) -> CampaignPageResponse:
        # Scoped to the requesting HR_ADMIN's own campaigns (created_by from
        # their token) and paginated, 6 per page by default — this endpoint
        # no longer returns every campaign in the org in one shot.
        campaigns, total = self.campaign_repo.get_campaigns_by_created_by(
            created_by=created_by, show_closed=show_closed, search=search, status=status,
            page=page, page_size=page_size,
        )
        cap_warning_percentage, deadline_warning_days = self._get_warning_thresholds()
        hm_review_sla_days, stale_campaign_days = self._get_review_stall_thresholds()
        hm_names = self._hiring_manager_name_map(campaigns)
        prompt_names = self._prompt_name_map(campaigns)
        aggregates = self._campaign_aggregate_maps(campaigns, hm_review_sla_days, stale_campaign_days)
        items = [
            CampaignResponse(id=c.id,
                name=c.name,
                status=c.status.value,
                jd_title=c.job_description.title,
                jd_version=c.job_description.version_number,
                hiring_manager=hm_names.get(c.hiring_manager_id, c.hiring_manager_id),
                max_candidates=c.max_candidates,
                deadline=c.deadline,
                created_at=c.created_at,
                prompt_template_id=c.prompt_template_id,
                prompt_name=prompt_names.get(c.prompt_template_id),
                candidate_count=aggregates["candidate_counts"].get(c.id, 0),
                shortlisted_count=aggregates["shortlisted_counts"].get(c.id, 0),
                approaching_cap=self._is_approaching_cap(aggregates["selected_counts"].get(c.id, 0),
                    c.max_candidates,
                    cap_warning_percentage,
                ),
                deadline_soon=self._is_deadline_soon(c.deadline,
                    deadline_warning_days,
                ),
                overdue_review=aggregates["overdue_review_counts"].get(c.id, 0) > 0,
                pipeline_stalled=aggregates["pipeline_stalled_map"].get(c.id, False),
            )
            for c in campaigns
        ]
        return CampaignPageResponse(items=items, page=page, page_size=page_size, total=total)

    def get_all_campaigns_for_hiring_manager(self, manager_id: UUID, show_closed: bool = False) -> list[CampaignResponse]:
        if not self.cache_service:
            return self._load_all_campaigns_for_hiring_manager(manager_id, show_closed)
        raw = self.cache_service.get_or_set(
            campaign_list_key({"kind": "hiring-manager", "manager_id": str(manager_id), "show_closed": show_closed}),
            loader=lambda: json.dumps(
                [c.model_dump(mode="json") for c in self._load_all_campaigns_for_hiring_manager(manager_id, show_closed)]
            ),
            ttl=settings.cache_campaign_list_ttl_seconds,
        )
        return [CampaignResponse.model_validate(item) for item in json.loads(raw)]

    def _load_all_campaigns_for_hiring_manager(
        self, manager_id: UUID, show_closed: bool,
    ) -> list[CampaignResponse]:
        campaigns = self.campaign_repo.get_all_campaigns_for_hiring_manager(manager_id, show_closed=show_closed)
        cap_warning_percentage, deadline_warning_days = self._get_warning_thresholds()
        hm_review_sla_days, stale_campaign_days = self._get_review_stall_thresholds()
        hm_names = self._hiring_manager_name_map(campaigns)
        prompt_names = self._prompt_name_map(campaigns)
        aggregates = self._campaign_aggregate_maps(campaigns, hm_review_sla_days, stale_campaign_days)
        return [
            CampaignResponse(id=c.id,
                name=c.name,
                status=c.status.value,
                jd_title=c.job_description.title,
                jd_version=c.job_description.version_number,
                hiring_manager=hm_names.get(c.hiring_manager_id, c.hiring_manager_id),
                max_candidates=c.max_candidates,
                deadline=c.deadline,
                created_at=c.created_at,
                prompt_template_id=c.prompt_template_id,
                prompt_name=prompt_names.get(c.prompt_template_id),
                candidate_count=aggregates["candidate_counts"].get(c.id, 0),
                shortlisted_count=aggregates["shortlisted_counts"].get(c.id, 0),
                approaching_cap=self._is_approaching_cap(aggregates["selected_counts"].get(c.id, 0),
                    c.max_candidates,
                    cap_warning_percentage,
                ),
                deadline_soon=self._is_deadline_soon(c.deadline,
                    deadline_warning_days,
                ),
                overdue_review=aggregates["overdue_review_counts"].get(c.id, 0) > 0,
                pipeline_stalled=aggregates["pipeline_stalled_map"].get(c.id, False),
            )
            for c in campaigns
        ]


    def search_campaigns(self,
        filters: CampaignFilterRequest,
        requesting_user: TokenUser | None = None,
    ) -> list[CampaignResponse]:

        if requesting_user is not None and UserRole.HIRING_MANAGER.value in requesting_user.roles:
            # a HIRING_MANAGER must never see campaigns beyond their
            # own, regardless of what hiring_manager_id filter was requested.
            filters.hiring_manager_id = requesting_user.user_id

        if not self.cache_service:
            return self._load_search_campaigns(filters)
        raw = self.cache_service.get_or_set(
            campaign_list_key({"kind": "search", **filters.model_dump(mode="json")}),
            loader=lambda: json.dumps(
                [c.model_dump(mode="json") for c in self._load_search_campaigns(filters)]
            ),
            ttl=settings.cache_campaign_list_ttl_seconds,
        )
        return [CampaignResponse.model_validate(item) for item in json.loads(raw)]

    def _load_search_campaigns(self, filters: CampaignFilterRequest) -> list[CampaignResponse]:
        campaigns = self.campaign_repo.search_campaigns(filters)
        cap_warning_percentage, deadline_warning_days = self._get_warning_thresholds()
        hm_review_sla_days, stale_campaign_days = self._get_review_stall_thresholds()
        hm_names = self._hiring_manager_name_map(campaigns)
        prompt_names = self._prompt_name_map(campaigns)
        aggregates = self._campaign_aggregate_maps(campaigns, hm_review_sla_days, stale_campaign_days)

        return [
            CampaignResponse(id=c.id,
                name=c.name,
                status=c.status.value,
                jd_title=c.job_description.title,
                jd_version=c.job_description.version_number,
                hiring_manager=hm_names.get(c.hiring_manager_id, c.hiring_manager_id),
                deadline=c.deadline,
                max_candidates=c.max_candidates,
                created_at=c.created_at,
                prompt_template_id=c.prompt_template_id,
                prompt_name=prompt_names.get(c.prompt_template_id),
                candidate_count=aggregates["candidate_counts"].get(c.id, 0),
                shortlisted_count=aggregates["shortlisted_counts"].get(c.id, 0),
                approaching_cap=self._is_approaching_cap(aggregates["selected_counts"].get(c.id, 0),
                    c.max_candidates,
                    cap_warning_percentage,
                ),
                deadline_soon=self._is_deadline_soon(c.deadline,
                    deadline_warning_days,
                ),
                overdue_review=aggregates["overdue_review_counts"].get(c.id, 0) > 0,
                pipeline_stalled=aggregates["pipeline_stalled_map"].get(c.id, False),
            )
            for c in campaigns
        ]

    def update_scoring_configuration(self,
        campaign_id: UUID,
        request: CampaignScoringUpdateRequest,
        updated_by: str,
    ) -> CampaignScoringConfigurationResponse:
        """
        Updates a campaign's scoring weights/thresholds. M10-E02: wrapped in
        a single transaction (validate -> persist campaign -> persist weight
        -configuration history -> audit -> commit -> best-effort Composite
        Score recalculation) - any failure before commit rolls back every
        write together, so a history row or audit entry can never be
        persisted without the campaign update actually landing, or vice
        versa. Uses get_by_id_for_update (a locking SELECT ... FOR UPDATE,
        already used elsewhere for campaign-row races) so two concurrent
        weight-change requests against the same campaign serialize instead
        of racing to commit.
        """
        try:
            campaign = self.campaign_repo.get_by_id_for_update(campaign_id)
            if not campaign:
                raise CampaignException(f"Campaign with ID '{campaign_id}' not found",
                    404,
                    None,
                )

            self._validate_scoring_weights(request.weight_deterministic,
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

            # M10-E02: a genuine weight change (as opposed to a
            # thresholds-only change, or an exact resubmission of the
            # current weights - the no-op case) - captured BEFORE
            # update_scoring_configuration() below mutates `campaign` in
            # place, so this is the true "before" snapshot for history/audit.
            weight_fields_changed = _WEIGHT_FIELDS & changes.keys()
            old_weights = None
            if weight_fields_changed:
                old_weights = {
                    "weight_deterministic": Decimal(str(campaign.weight_deterministic)),
                    "weight_semantic": Decimal(str(campaign.weight_semantic)),
                    "weight_ai": Decimal(str(campaign.weight_ai)),
                }

            candidate_count = self.campaign_repo.get_candidate_count(campaign.id)

            campaign = (self.campaign_repo.update_scoring_configuration(campaign,
                    request,
                )
            )

            if changes:
                # same action_type update_campaign() uses for scoring edits,
                # so both edit paths land in the same Weight Change History query.
                self.audit_service.log(actor_id=updated_by,
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

            # M10-E02 Story 2: history + dedicated audit entry, only for an
            # actual weight change - never for a thresholds-only change, and
            # never for a no-op resubmission of identical weights (in which
            # case weight_fields_changed is empty and this is skipped
            # entirely, per the No-Op Detection requirement).
            if weight_fields_changed:
                self._record_weight_configuration_change(campaign, old_weights, updated_by)

            self.campaign_repo.commit()
            self._invalidate_campaign_caches(campaign.id)

            if weight_fields_changed:
                self._enqueue_composite_recalculation_for_campaign(campaign.id)

            result = self.get_scoring_configuration(campaign.id)
            result.warning = self._already_processed_warning(candidate_count)

            return result
        except Exception:
            self.campaign_repo.rollback()
            raise

    def get_weight_presets(self,
        org_id: UUID,
    ) -> list[CampaignWeightPresetResponse]:
        if not self.cache_service:
            return self._load_weight_presets(org_id)

        raw = self.cache_service.get_or_set(
            campaign_weight_presets_key(org_id),
            loader=lambda: json.dumps(
                [preset.model_dump(mode="json") for preset in self._load_weight_presets(org_id)]
            ),
            ttl=settings.cache_campaign_ttl_seconds,
        )
        return [CampaignWeightPresetResponse.model_validate(item) for item in json.loads(raw)]

    def _load_weight_presets(self, org_id: UUID) -> list[CampaignWeightPresetResponse]:

        system_presets = [
            CampaignWeightPresetResponse(id=UUID("00000000-0000-0000-0000-000000000001"),
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
            CampaignWeightPresetResponse(id=UUID("00000000-0000-0000-0000-000000000002"),
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
            CampaignWeightPresetResponse(id=UUID("00000000-0000-0000-0000-000000000003"),
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
            CampaignWeightPresetResponse(id=UUID("00000000-0000-0000-0000-000000000004"),
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

        custom_presets = self.preset_repo.get_all_by_org(org_id
        )

        preset_responses = [
            CampaignWeightPresetResponse.model_validate(preset
            )
            for preset in custom_presets
        ]

        return system_presets + preset_responses
    
    def create_weight_preset(self,
        request: CampaignWeightPresetCreateRequest,
        org_id: UUID,
        created_by: str,
    ) -> CampaignWeightPresetResponse:

        existing_preset = self.preset_repo.get_by_name(org_id=org_id,
            name=request.name,
        )

        if existing_preset:
            raise CampaignException(f"Preset '{request.name}' already exists.",
                400,
                None,
            )

        self._validate_scoring_weights(request.weight_deterministic,
            request.weight_semantic,
            request.weight_ai,
        )

        preset = CampaignWeightPreset(org_id=org_id,
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

        try:
            preset = self.preset_repo.create(preset)

            self.audit_service.log(actor_id=created_by,
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

            self.preset_repo.commit()
            if self.cache_service:
                self.cache_service.delete(campaign_weight_presets_key(org_id))
        except Exception:
            self.preset_repo.rollback()
            raise

        return CampaignWeightPresetResponse.model_validate(preset
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

    def update_weight_preset(self,
        preset_id: UUID,
        request: CampaignWeightPresetUpdateRequest,
        org_id: UUID,
        updated_by: str,
    ) -> CampaignWeightPresetResponse:

        if preset_id in self._SYSTEM_PRESET_IDS:
            raise CampaignException("System presets are read-only and cannot be modified.",
                403,
                None,
            )

        preset = self.preset_repo.get_by_id(preset_id
        )

        if not preset:
            raise CampaignException("Weight preset not found.",
                404,
                None,
            )

        if preset.org_id != org_id:
            raise CampaignException("Weight preset not found.",
                404,
                None,
            )

        duplicate = self.preset_repo.get_by_name(org_id=org_id,
            name=request.name,
        )

        if duplicate and duplicate.id != preset.id:
            raise CampaignException(f"Preset '{request.name}' already exists.",
                400,
                None,
            )

        self._validate_scoring_weights(request.weight_deterministic,
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

        try:
            preset = self.preset_repo.update(preset
            )

            self.audit_service.log(actor_id=updated_by,
                actor_role="HR_ADMIN",
                action_type=ActionType.CAMPAIGN_WEIGHT_PRESET_UPDATED.value,
                entity_type=EntityType.CAMPAIGN_WEIGHT_PRESET.value,
                entity_id=preset.id,
                details={
                    "title": f"Updated preset '{preset.name}'"
                },
            )

            self.preset_repo.commit()
            if self.cache_service:
                self.cache_service.delete(campaign_weight_presets_key(org_id))
        except Exception:
            self.preset_repo.rollback()
            raise

        return CampaignWeightPresetResponse.model_validate(preset
        )
    
    def delete_weight_preset(self,
        preset_id: UUID,
        org_id: UUID,
        deleted_by: str,
    ) -> None:

        if preset_id in self._SYSTEM_PRESET_IDS:
            raise CampaignException("System presets are read-only and cannot be deleted.",
                403,
                None,
            )

        preset = self.preset_repo.get_by_id(preset_id
        )

        if not preset:
            raise CampaignException("Weight preset not found.",
                404,
                None,
            )

        if preset.org_id != org_id:
            raise CampaignException("Weight preset not found.",
                404,
                None,
            )

        try:
            self.preset_repo.delete(preset
            )

            self.audit_service.log(actor_id=deleted_by,
                actor_role="HR_ADMIN",
                action_type=ActionType.CAMPAIGN_WEIGHT_PRESET_DELETED.value,
                entity_type=EntityType.CAMPAIGN_WEIGHT_PRESET.value,
                entity_id=preset.id,
                details={
                    "title": f"Deleted preset '{preset.name}'"
                },
            )

            self.preset_repo.commit()
            if self.cache_service:
                self.cache_service.delete(campaign_weight_presets_key(org_id))
        except Exception:
            self.preset_repo.rollback()
            raise

    def get_campaign_details(self,campaign_id: UUID, user:TokenUser) -> CampaignDetailResponse:
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException(f"Campaign '{campaign_id}' not found",404)

        jd = self.jd_repo.get_by_id(campaign.jd_id)
        if not jd:
            raise CampaignException("Associated job description not found", 404)

        creator = self.campaign_repo.get_user(campaign.created_by)
        manager = (self.campaign_repo.get_user(campaign.hiring_manager_id)
            if campaign.hiring_manager_id
            else None
        )

        is_hiring_manager_only = (UserRole.HIRING_MANAGER.value in user.roles
            and UserRole.HR_ADMIN.value not in user.roles
            and UserRole.RECRUITER.value not in user.roles
        )

        # "Duplicated from [source name]" — sourced from the
        # CAMPAIGN_DUPLICATED audit entry rather than a dedicated column,
        # reusing the same audit-lookup pattern as pause-duration/reopen.
        duplication_entry = self.audit_service.get_latest_entry(campaign.id, ActionType.CAMPAIGN_DUPLICATED.value,
        )
        duplicated_from_id = None
        duplicated_from_name = None
        if duplication_entry is not None:
            detail = duplication_entry.detail or {}
            duplicated_from_id = detail.get("source_campaign_id")
            duplicated_from_name = detail.get("source_campaign_name")

        return CampaignDetailResponse(id=campaign.id,
            campaign_info=CampaignInfoSection(name=campaign.name,
                status=campaign.status.value,
                created_by_name=creator.full_name if creator else None,
                created_at=campaign.created_at,
                updated_at=campaign.updated_at,
                duplicated_from_campaign_id=duplicated_from_id,
                duplicated_from_campaign_name=duplicated_from_name,
            ),
            jd_configuration=JDConfigSection(jd_id=jd.id,
                jd_title=jd.title,
                version_number=jd.version_number,
                jurisdiction=jd.jurisdiction,
                mandatory_skill_count=self.campaign_repo.get_mandatory_skill_count(jd.id),
            ),
            # role gate: spec says HM must NOT see weights or manager contact
            scoring_configuration=None if is_hiring_manager_only else ScoringConfigSection(weight_deterministic=campaign.weight_deterministic,
                weight_semantic=campaign.weight_semantic,
                weight_ai=campaign.weight_ai,
                semantic_threshold=campaign.semantic_threshold,
                ai_threshold=campaign.ai_threshold,
                deterministic_threshold=campaign.deterministic_threshold,
            ),
            pipeline_limits=PipelineLimitsSection(max_candidates=campaign.max_candidates,
                current_candidate_count=self.campaign_repo.get_candidate_count(campaign.id),
                selected_count=self.campaign_repo.get_selected_count(campaign.id),
                deadline=campaign.deadline,
            ),
            hiring_manager=(None if is_hiring_manager_only else (HiringManagerSection(full_name=manager.full_name,
                email=manager.email,
            ) if manager else None)),
        )

    # The true sequential funnel: a candidate normally passes through these in
    # order, so "drop-off between each stage" is meaningful here.
    _FUNNEL_STAGES = (PipelineStage.UPLOADED,
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
    _BUCKET_STAGES = (PipelineStage.HOLD,
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

        return PipelineSummaryResponse(campaign_id=campaign_id,
            total_candidates=sum(counts.values()),
            stages=stages,
        )

    def get_processing_status_summary(self, campaign_id: UUID) -> ProcessingStatusSummaryResponse:
        """+ status breakdown + DLQ count + completion estimate."""
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException(f"Campaign '{campaign_id}' not found", 404)

        counts = self.campaign_repo.get_task_status_counts(campaign_id)
        dlq_count = len(self.campaign_repo.get_dead_letter_queue_entries(campaign_id))

        breakdown = self.campaign_repo.get_task_type_breakdown(campaign_id)
        estimate = self._estimate_completion(breakdown, self._circuit_breaker_summaries())

        return ProcessingStatusSummaryResponse(queued_count=counts.get(TaskStatus.QUEUED.value, 0),
            running_count=counts.get(TaskStatus.RUNNING.value, 0),
            retry_count=counts.get(TaskStatus.RETRY.value, 0),
            dead_count=counts.get(TaskStatus.DEAD.value, 0),
            paused_count=counts.get(TaskStatus.PAUSED.value, 0),
            dead_letter_queue_count=dlq_count,
            estimated_completion=estimate,
        )

    def get_dead_letter_queue_for_campaign(self,
        campaign_id: UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> DeadLetterQueuePageResponse:
        """Paginated DLQ entries, narrowed to task_types this endpoint can actually replay."""
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException(f"Campaign '{campaign_id}' not found", 404)

        entries, total = self.campaign_repo.get_replayable_dead_letter_queue_page(
            campaign_id, list(self._DLQ_REPLAY_BUILDERS), limit, offset,
        )
        return DeadLetterQueuePageResponse(
            entries=[
                DeadLetterQueueEntryResponse(id=e.id,
                    task_type=e.task_type,
                    final_error_message=e.final_error_message,
                    retry_count=e.retry_count,
                    moved_to_dlq_at=e.moved_to_dlq_at,
                    campaign_candidate_id=e.campaign_candidate_id,
                    last_attempted_at=e.last_attempted_at,
                    resolution_notes=e.resolution_notes,
                    replayed_at=e.replayed_at,
                    replay_supported=True,
                )
                for e in entries
            ],
            total=total,
            limit=limit,
            offset=offset,
        )

    # ── Processing Queue ────────────────────────────────────────────

    # Services whose OPEN breaker means candidate processing is genuinely
    # stalled. NOTE: only SUPABASE_STORAGE / ENCRYPTION_SERVICE are actually
    # written to circuit_breaker_state today (M05 Phase 11 scope) — the
    # GEMINI_FLASH / EMBEDDING_SERVICE entries the spec names will read
    # CLOSED until those callers start recording failures (owned by M05/M06).
    _MONITORED_BREAKER_SERVICES = ("GEMINI_FLASH",
        "EMBEDDING_SERVICE",
        "SUPABASE_STORAGE",
        "ENCRYPTION_SERVICE",
    )
    _ACTIVE_TASK_STATUSES = ("QUEUED", "RUNNING", "RETRY")

    def _circuit_breaker_summaries(self) -> list[CircuitBreakerSummaryResponse]:
        # One query for all monitored services rather than one per service.
        rows = self.circuit_breaker_repo.get_by_service_names(self._MONITORED_BREAKER_SERVICES)
        summaries = []
        for name in self._MONITORED_BREAKER_SERVICES:
            row = rows.get(name)
            summaries.append(CircuitBreakerSummaryResponse(service_name=name,
                state=row.state.value if row else "CLOSED",  # absent row == never failed
                failure_count=row.failure_count if row else 0,
                opened_at=row.opened_at if row else None,
                retry_after=row.retry_after if row else None,
            ))
        return summaries

    def get_processing_queue(self, campaign_id: UUID) -> ProcessingQueueResponse:
        """+ T03: task-type breakdown, breaker states, completion estimate."""
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException(f"Campaign '{campaign_id}' not found", 404)

        breakdown = self.campaign_repo.get_task_type_breakdown(campaign_id)
        breakers = self._circuit_breaker_summaries()

        return ProcessingQueueResponse(task_types=[TaskTypeBreakdownResponse(**b) for b in breakdown],
            circuit_breakers=breakers,
            estimated_completion=self._estimate_completion(breakdown, breakers),
        )

    def _estimate_completion(self,
        breakdown: list[dict],
        breakers: list[CircuitBreakerSummaryResponse],
    ) -> EstimatedCompletionResponse:
        """
        Σ per-type (remaining × avg duration of that type's completed
        tasks). The range is ±25% of the point estimate — honest about queue
        variance without pretending worker-level precision we don't have.
        """
        open_breaker = next((b for b in breakers if b.state == "OPEN"), None)

        remaining_total = 0
        estimated_seconds = 0.0
        missing_history = False
        for entry in breakdown:
            remaining = sum(entry["status_counts"].get(s, 0) for s in self._ACTIVE_TASK_STATUSES
            )
            if remaining == 0:
                continue
            remaining_total += remaining
            if entry["avg_duration_ms"] is None:
                # tasks pending, but nothing of this type has ever completed —
                # no defensible basis for an estimate
                missing_history = True
                continue
            estimated_seconds += remaining * (entry["avg_duration_ms"] / 1000.0)

        if remaining_total == 0:
            return EstimatedCompletionResponse(remaining_task_count=0,
                estimate_available=False,
                message="No tasks queued — processing is up to date.",
            )
        if open_breaker is not None:
            return EstimatedCompletionResponse(remaining_task_count=remaining_total,
                estimate_available=False,
                message="Processing paused — external service degraded.",
            )
        if missing_history or estimated_seconds <= 0:
            return EstimatedCompletionResponse(remaining_task_count=remaining_total,
                estimate_available=False,
                message="Completion time unavailable — check back shortly.",
            )

        lo = max(1, math.ceil(estimated_seconds * 0.75 / 60))
        hi = max(lo, math.ceil(estimated_seconds * 1.25 / 60))
        return EstimatedCompletionResponse(remaining_task_count=remaining_total,
            estimate_available=True,
            min_minutes=lo,
            max_minutes=hi,
            message=f"Estimated completion: {lo} to {hi} minutes",
        )

    # ── DLQ replay ──────────────────────────────────────────────
    # dead_letter_queue.input_payload holds the retry CHECKPOINT's context
    # (stage data), NOT task kwargs — so kwargs are rebuilt from the DLQ
    # row's own linkage columns. Each replayed task creates its own new
    # celery_task_log row on entry (the codebase-wide pattern), which is
    # exactly the INSERT the spec asks for.
    #
    # BULK_RESUME_PARSE is deliberately absent: those failures are replayed
    # per-file from the bulk-upload screen (POST /bulk-uploads/.../replay),
    # which owns the job/file counter bookkeeping a bare re-enqueue would skip.
    _DLQ_REPLAY_BUILDERS = {
        DETERMINISTIC_SCORE_TASK_TYPE: lambda e: (calculate_deterministic_score_task,
            {"campaign_candidate_id": str(e.campaign_candidate_id)},
            e.campaign_candidate_id is not None,
        ),
        "RESUME_DOCUMENT_PROCESSING": lambda e: (process_resume_document,
            {"resume_id": str(e.resume_id)},
            e.resume_id is not None,
        ),
        EMBED_RESUME_TASK_TYPE: lambda e: (generate_resume_embedding_task,
            {"resume_id": str(e.resume_id)},
            e.resume_id is not None,
        ),
        AI_EVALUATE_TASK_TYPE: lambda e: (calculate_ai_evaluation_task,
            {"campaign_candidate_id": str(e.campaign_candidate_id)},
            e.campaign_candidate_id is not None,
        ),
    }

    def _embed_resume_replay_skip_reason(self, entry) -> str | None:
        """
        EMBED_RESUME-specific pre-checks before replaying a DEAD entry -
        none of the other registered task types need this (they re-validate
        entirely inside their own task on execution), but re-running the
        embedding model is comparatively expensive, so it's worth confirming
        there's still something to do before dispatching:
        - the resume must still exist
        - it must still have parsed_json (generate_resume_embedding_task
          raises ValueError and dead-letters again immediately otherwise)
        - it must not already have an embedding (e.g. resolved through a
          different path since this entry was dead-lettered)
        Returns None when the replay should proceed.
        """
        resume = self.resume_repo.get_by_id(entry.resume_id)
        if resume is None:
            return "Resume no longer exists."
        if not resume.parsed_json or not isinstance(resume.parsed_json, dict):
            return "Resume has no parsed_json - nothing available to embed."
        if self.resume_repo.get_embedding(entry.resume_id) is not None:
            return "Resume embedding already exists - nothing to replay."
        return None

    def replay_dead_letter_tasks(self,
        campaign_id: UUID,
        dlq_ids: list[UUID],
        replayed_by: str,
        actor_role: str | None,
    ) -> DLQReplayResponse:
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException(f"Campaign '{campaign_id}' not found", 404)

        max_replays = int(self.config_repo.get_configs_by_keys(["MAX_DLQ_REPLAYS_PER_TASK"])
            .get("MAX_DLQ_REPLAYS_PER_TASK") or 3
        )

        entries = self.campaign_repo.get_dlq_entries_by_ids(campaign_id, dlq_ids)
        found_ids = {e.id for e in entries}
        results: list[DLQReplayResultItem] = []

        for missing in set(dlq_ids) - found_ids:
            results.append(DLQReplayResultItem(dlq_id=missing, status="SKIPPED",
                reason="Entry not found for this campaign.",
            ))

        to_enqueue = []  # (task_fn, kwargs, new_task_id) — fired only after commit
        for entry in entries:
            if entry.replayed_at is not None:
                results.append(DLQReplayResultItem(dlq_id=entry.id, status="SKIPPED", reason="Already replayed.",
                ))
                continue
            builder = self._DLQ_REPLAY_BUILDERS.get(entry.task_type)
            if builder is None:
                results.append(DLQReplayResultItem(dlq_id=entry.id, status="SKIPPED",
                    reason=f"Replay not supported for task type '{entry.task_type}'.",
                ))
                continue
            task_fn, kwargs, linkable = builder(entry)
            if not linkable:
                results.append(DLQReplayResultItem(dlq_id=entry.id, status="SKIPPED",
                    reason="Entry has no entity reference to rebuild the task from.",
                ))
                continue
            if entry.task_type == EMBED_RESUME_TASK_TYPE:
                skip_reason = self._embed_resume_replay_skip_reason(entry)
                if skip_reason is not None:
                    results.append(DLQReplayResultItem(dlq_id=entry.id, status="SKIPPED", reason=skip_reason))
                    continue
            if self.campaign_repo.count_dlq_chain(entry) >= max_replays:
                results.append(DLQReplayResultItem(dlq_id=entry.id, status="SKIPPED",
                    reason=f"Replay limit reached ({max_replays}).",
                ))
                continue

            new_task_id = str(uuid4())
            self.dead_letter_queue_repo.mark_replayed(entry.id,
                replayed_by=replayed_by,
                replayed_at=datetime.now(timezone.utc),
            )
            self.audit_service.log(actor_id=replayed_by,
                actor_role=actor_role,
                action_type=ActionType.DLQ_TASK_REPLAYED,
                entity_type=EntityType.DEAD_LETTER_QUEUE,
                entity_id=entry.id,
                campaign_id=campaign_id,
                details={
                    "task_type": entry.task_type,
                    "original_task_id": entry.original_task_id,
                    "new_task_id": new_task_id,
                    "final_error_message": entry.final_error_message[:500],
                },
            )
            to_enqueue.append((task_fn, kwargs, new_task_id))
            results.append(DLQReplayResultItem(dlq_id=entry.id, status="REPLAYED", new_task_id=new_task_id,
            ))

        # commit DLQ marks + audit rows BEFORE enqueueing, so a broker hiccup
        # can't leave enqueued tasks whose bookkeeping was rolled back
        self.campaign_repo.commit()

        for task_fn, kwargs, new_task_id in to_enqueue:
            task_fn.apply_async(kwargs=kwargs, task_id=new_task_id)

        replayed = sum(1 for r in results if r.status == "REPLAYED")
        return DLQReplayResponse(replayed_count=replayed,
            skipped_count=len(results) - replayed,
            results=results,
        )

    # ── Stalled candidates ──────────────────────────────────────────

    def _get_stall_slas(self) -> dict[str, float]:
        configs = self.config_repo.get_configs_by_keys([
            "SCREENING_SLA_HOURS", "HM_REVIEW_SLA_DAYS", "INTERVIEW_SLA_DAYS",
        ])
        return {
            "screening_sla_hours": float(configs.get("SCREENING_SLA_HOURS", "48")),
            "hm_review_sla_days": float(configs.get("HM_REVIEW_SLA_DAYS", "5")),
            "interview_sla_days": float(configs.get("INTERVIEW_SLA_DAYS", "7")),
        }

    def get_stalled_candidates(self, campaign_id: UUID) -> StalledCandidatesResponse:
        """the computed stalled-candidates 'view' for one campaign."""
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException(f"Campaign '{campaign_id}' not found", 404)

        slas = self._get_stall_slas()
        rows = self.campaign_repo.get_stalled_candidates(campaign_id,
            screening_sla_hours=slas["screening_sla_hours"],
            hm_review_sla_days=slas["hm_review_sla_days"],
            interview_sla_days=slas["interview_sla_days"],
        )

        # Frontend follow-up: candidate_name added to what was originally a
        # deliberately anonymous response - decrypted the same way every
        # other campaign-wide list in this codebase does (see
        # CampaignCandidateService._decrypt_candidate_name /
        # InterviewScheduleService.get_campaign_interviews).
        # EncryptionService/CandidateRepository constructed ad-hoc rather
        # than added to this class's already-large constructor, matching
        # that same convention for an occasional-use dependency.
        encryption_service = EncryptionService(EncryptionKeyRepository(self.db))
        candidates_by_id = {
            c.id: c for c in CandidateRepository(self.db).get_by_ids([r["candidate_id"] for r in rows])
        }

        items = []
        for r in rows:
            candidate = candidates_by_id.get(r.pop("candidate_id"))
            candidate_name = (
                encryption_service.decrypt(candidate.full_name_encrypted, candidate.encryption_key_id)
                if candidate is not None else "Unknown"
            )
            items.append(StalledCandidateItem(candidate_name=candidate_name, **r))

        return StalledCandidatesResponse(items=items,
            total=len(items),
            sla_config=slas,
        )

    def _get_stalled_candidate_or_raise(self, campaign_id: UUID, campaign_candidate_id: UUID):
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException(f"Campaign '{campaign_id}' not found", 404)
        cc = self.campaign_repo.get_campaign_candidate(campaign_id, campaign_candidate_id)
        if cc is None:
            raise CampaignException(f"Candidate '{campaign_candidate_id}' not found in this campaign.", 404,
            )
        return cc

    def reprocess_stalled_candidate(self, campaign_id: UUID, campaign_candidate_id: UUID, actor_id: str, actor_role: str | None,
    ) -> StalledActionResponse:
        """Re-Process: replays this candidate's dead-lettered tasks (delegates to the S03 replay engine — same limits, same audit)."""
        cc = self._get_stalled_candidate_or_raise(campaign_id, campaign_candidate_id)

        dlq_ids = self.campaign_repo.get_unreplayed_dlq_ids_for_candidate(cc.id)
        if not dlq_ids:
            raise CampaignException("No replayable failed tasks found for this candidate.", 409,
            )
        replay = self.replay_dead_letter_tasks(campaign_id, dlq_ids, replayed_by=actor_id, actor_role=actor_role,
        )
        return StalledActionResponse(campaign_candidate_id=cc.id,
            action="REPROCESSED",
            detail=f"Replayed {replay.replayed_count} task(s), skipped {replay.skipped_count}.",
            replayed_count=replay.replayed_count,
            results=replay.results,
        )

    def escalate_stalled_candidate(self, campaign_id: UUID, campaign_candidate_id: UUID,
        request: EscalateStallRequest, actor_id: str, actor_role: str | None,
    ) -> StalledActionResponse:
        """Escalate to HM — audit-recorded; the reminder email itself is TODO."""
        cc = self._get_stalled_candidate_or_raise(campaign_id, campaign_candidate_id)
        if cc.pipeline_stage != PipelineStage.HM_REVIEW:
            raise CampaignException(f"Escalation applies to HM_REVIEW stalls; this candidate is in {cc.pipeline_stage.value}.",
                409,
            )
        campaign = self.campaign_repo.get_by_id(campaign_id)
        self.audit_service.log(actor_id=actor_id,
            actor_role=actor_role,
            action_type=ActionType.CANDIDATE_STALL_ESCALATED,
            entity_type=EntityType.CAMPAIGN_CANDIDATE,
            entity_id=cc.id,
            campaign_id=campaign_id,
            details={
                "title": "Stalled HM review escalated to hiring manager",
                "hiring_manager_id": campaign.hiring_manager_id,
                "note": request.note,
            },
        )
        self.campaign_repo.commit()
        # Email notification
        # TODO: send HM_REVIEW reminder email to the assigned hiring manager
        return StalledActionResponse(campaign_candidate_id=cc.id,
            action="ESCALATED",
            detail="Escalation recorded. Reminder email delivery is pending email integration.",
        )

    # Natural next stage for a manual override — REJECTED/FRAUD_REVIEW are
    # deliberately NOT reachable here (rejection has its own flow; fraud has
    # the dedicated flag endpoint below).
    _STAGE_OVERRIDE_NEXT = {
        PipelineStage.UPLOADED: PipelineStage.SCREENING,
        PipelineStage.SCREENING: PipelineStage.SHORTLISTED,
        PipelineStage.SHORTLISTED: PipelineStage.HM_REVIEW,
        PipelineStage.HM_REVIEW: PipelineStage.INTERVIEW,
        PipelineStage.INTERVIEW: PipelineStage.SELECTED,
        PipelineStage.HOLD: PipelineStage.HM_REVIEW,
    }
    _OVERRIDE_FORBIDDEN_TARGETS = {PipelineStage.REJECTED, PipelineStage.FRAUD_REVIEW, PipelineStage.UPLOADED}

    def override_candidate_stage(self, campaign_id: UUID, campaign_candidate_id: UUID,
        request: StageOverrideRequest, actor_id: str, actor_role: str | None,
    ) -> StalledActionResponse:
        """Override Stage: manual advance with mandatory reason."""
        cc = self._get_stalled_candidate_or_raise(campaign_id, campaign_candidate_id)
        from_stage = cc.pipeline_stage

        if request.target_stage:
            try:
                target = PipelineStage(request.target_stage)
            except ValueError:
                raise CampaignException(f"Unknown pipeline stage '{request.target_stage}'.", 422)
            if target in self._OVERRIDE_FORBIDDEN_TARGETS:
                raise CampaignException(f"Stage '{target.value}' cannot be set via override — use the dedicated flow.", 422,
                )
            if target == from_stage:
                raise CampaignException("Candidate is already in that stage.", 409)
        else:
            target = self._STAGE_OVERRIDE_NEXT.get(from_stage)
            if target is None:
                raise CampaignException(f"No natural next stage from {from_stage.value} — pass target_stage explicitly.", 409,
                )

        self.campaign_repo.transition_candidate_stage(cc, target,
            changed_by=actor_id,
            change_reason=request.reason,
            transition_source=TransitionSource.OVERRIDE,
        )
        if target == PipelineStage.INTERVIEW:
            # Same-transaction, same reasoning as the other two engines
            # that can reach to_stage=INTERVIEW - a plain INSERT on this
            # same session, get_or_create so a re-entry never gets a
            # second row, must roll back with the rest of this override
            # if it fails.
            self.interview_schedule_repo.get_or_create_pending(cc.id)
        if from_stage == PipelineStage.INTERVIEW and target in _INTERVIEW_EXIT_CASCADE_STAGES:
            # M12 cascading-cancellation hook - same constant, same
            # reasoning as StageTransitionService/PipelineTransitionService's
            # own copies (this is the third of the 3 places a candidate can
            # leave INTERVIEW from - override_candidate_stage can reach
            # SELECTED/SHORTLISTED via an explicit target_stage even though
            # neither is this stage's _STAGE_OVERRIDE_NEXT default).
            self.interview_schedule_repo.cancel_active_rounds(
                cc.id,
                reason=f"Candidate outcome finalized: {target.value}",
                changed_by=actor_id,
                changed_by_role=actor_role,
            )
        # Epic 5 follow-up - manual re-score trigger: arriving at
        # SCREENING from anywhere other than UPLOADED cancels any still-
        # active interview rounds, same transaction, same reasoning as
        # the cascade-cancel hook above.
        if target == PipelineStage.SCREENING and from_stage != PipelineStage.UPLOADED:
            self.interview_schedule_repo.cancel_active_rounds(
                cc.id,
                reason="Candidate returned to SCREENING for re-evaluation",
                changed_by=actor_id,
                changed_by_role=actor_role,
            )
        self.audit_service.log(actor_id=actor_id,
            actor_role=actor_role,
            action_type=ActionType.CANDIDATE_STAGE_OVERRIDDEN,
            entity_type=EntityType.CAMPAIGN_CANDIDATE,
            entity_id=cc.id,
            campaign_id=campaign_id,
            details={
                "title": "Pipeline stage manually overridden",
                "from_stage": from_stage.value,
                "to_stage": target.value,
                "reason": request.reason,
            },
        )
        # This is the second stage-writing path (PipelineTransitionService is
        # the other), and _STAGE_OVERRIDE_NEXT maps INTERVIEW -> SELECTED, so
        # the openings cap has to be honoured here too or overrides bypass it.
        if target == PipelineStage.SELECTED:
            self._close_if_all_positions_filled(campaign_id, actor_id, actor_role)
        self.campaign_repo.commit()

        # Selection email is no longer sent automatically here - see
        # CampaignCandidateService.send_selection_email (manual send
        # button, matching the "Send Rejection Email" precedent).
        # Epic 5 follow-up - manual re-score trigger, post-commit (see
        # manual_candidate_rescore.py). Never fires for the automated
        # UPLOADED->SCREENING path.
        if target == PipelineStage.SCREENING and from_stage != PipelineStage.UPLOADED:
            enqueue_manual_rescore(self.campaign_repo.db, cc)

        return StalledActionResponse(campaign_candidate_id=cc.id,
            action="STAGE_OVERRIDDEN",
            detail=f"Moved from {from_stage.value} to {target.value}.",
            from_stage=from_stage.value,
            to_stage=target.value,
        )

    def flag_candidate_for_review(self, campaign_id: UUID, campaign_candidate_id: UUID,
        request: FlagReviewRequest, actor_id: str, actor_role: str | None,
    ) -> StalledActionResponse:
        """Flag for Manual Review: routes the candidate to FRAUD_REVIEW."""
        cc = self._get_stalled_candidate_or_raise(campaign_id, campaign_candidate_id)
        from_stage = cc.pipeline_stage
        if from_stage == PipelineStage.FRAUD_REVIEW:
            raise CampaignException("Candidate is already in FRAUD_REVIEW.", 409)

        self.campaign_repo.transition_candidate_stage(cc, PipelineStage.FRAUD_REVIEW,
            changed_by=actor_id,
            change_reason=request.reason,
            transition_source=TransitionSource.MANUAL,
            set_fraud_flag=True,
        )
        self.audit_service.log(actor_id=actor_id,
            actor_role=actor_role,
            action_type=ActionType.CANDIDATE_FLAGGED_FOR_REVIEW,
            entity_type=EntityType.CAMPAIGN_CANDIDATE,
            entity_id=cc.id,
            campaign_id=campaign_id,
            details={
                "title": "Candidate flagged for manual review",
                "from_stage": from_stage.value,
                "reason": request.reason,
            },
        )
        self.campaign_repo.commit()
        return StalledActionResponse(campaign_candidate_id=cc.id,
            action="FLAGGED_FOR_REVIEW",
            detail=f"Moved from {from_stage.value} to FRAUD_REVIEW.",
            from_stage=from_stage.value,
            to_stage=PipelineStage.FRAUD_REVIEW.value,
        )

    # ── Rejection analytics ─────────────────────────────────────────

    def get_rejection_analytics(self, campaign_id: UUID) -> RejectionAnalyticsResponse:
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException(f"Campaign '{campaign_id}' not found", 404)

        total_candidates = self.campaign_repo.get_candidate_count(campaign_id)
        layer_breakdown = self.campaign_repo.get_rejection_layer_breakdown(campaign_id)
        total_rejections = sum(layer_breakdown.values())

        raw_reasons = self.campaign_repo.get_top_rejection_reasons(campaign_id, limit=10)
        top_reasons = [
            RejectionReasonItem(reason=r["reason"],
                count=r["count"],
                percentage=round(r["count"] / total_rejections * 100, 1) if total_rejections else 0.0,
            )
            for r in raw_reasons
        ]

        det_total = layer_breakdown.get(DecisionSource.DETERMINISTIC.value, 0)
        missing = [
            MissingSkillItem(canonical_name=name,
                count=count,
                percentage_of_deterministic=round(count / det_total * 100, 1) if det_total else 0.0,
            )
            for name, count in self.campaign_repo.get_missing_mandatory_skill_counts(campaign_id)
        ]

        configs = self.config_repo.get_configs_by_keys([
            "DETERMINISTIC_HIGH_REJECTION_THRESHOLD",
            "SEMANTIC_HIGH_REJECTION_THRESHOLD",
            "AI_HIGH_REJECTION_THRESHOLD",
            "MIN_CANDIDATES_FOR_ANALYTICS",
        ])
        min_candidates = int(configs.get("MIN_CANDIDATES_FOR_ANALYTICS", "10"))
        analytics_ready = total_candidates >= min_candidates

        recommendations: list[RejectionRecommendation] = []
        if analytics_ready and total_candidates > 0:
            thresholds = [
                (DecisionSource.DETERMINISTIC.value,
                    float(configs.get("DETERMINISTIC_HIGH_REJECTION_THRESHOLD", "60.00")),
                    "High deterministic rejection rate — review the JD's mandatory skills or lower the experience requirement.",
                    "REVIEW_JD_SKILLS",
                ),
                (DecisionSource.SEMANTIC.value,
                    float(configs.get("SEMANTIC_HIGH_REJECTION_THRESHOLD", "40.00")),
                    "High semantic rejection rate — consider lowering the campaign's semantic_threshold.",
                    "ADJUST_THRESHOLD",
                ),
                (DecisionSource.AI.value,
                    float(configs.get("AI_HIGH_REJECTION_THRESHOLD", "40.00")),
                    "High AI rejection rate — consider lowering ai_threshold or reviewing the active prompt version.",
                    "REVIEW_PROMPT",
                ),
            ]
            for layer, threshold, message, action in thresholds:
                rate = layer_breakdown.get(layer, 0) / total_candidates * 100
                if rate > threshold:
                    recommendations.append(RejectionRecommendation(condition=f"{layer}_REJECTION_RATE_HIGH",
                        layer=layer,
                        rate_pct=round(rate, 1),
                        threshold_pct=threshold,
                        recommendation=message,
                        action=action,
                    ))

        return RejectionAnalyticsResponse(total_candidates=total_candidates,
            total_rejections=total_rejections,
            layer_breakdown=layer_breakdown,
            top_reasons=top_reasons,
            top_missing_skill=missing[0] if missing else None,
            missing_skills=missing,
            analytics_ready=analytics_ready,
            min_candidates_required=min_candidates,
            recommendations=recommendations,
        )

    def get_campaign_timeline(self,
        campaign_id: UUID,
        limit: int = 20,
        offset: int = 0,
        event_type: str | None = None,
    ) -> CampaignTimelineResponse:
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException(f"Campaign '{campaign_id}' not found", 404)

        audit_logs = self.campaign_repo.get_audit_entries(campaign_id)
        stage_history = self.campaign_repo.get_stage_history(campaign_id)
        bulk_jobs = self.campaign_repo.get_bulk_upload_events(campaign_id)

        # one batched name lookup for every distinct actor across all three
        # sources — _resolve_actor per event was an N+1 (one query per row)
        actor_ids = {str(a) for a in (
                [log.actor_id for log in audit_logs]
                + [h.changed_by for h in stage_history]
                + [job.uploaded_by for job in bulk_jobs]
            ) if a
        }
        actor_names = self.campaign_repo.get_user_names(list(actor_ids))

        def resolve(actor_id) -> str:
            if not actor_id:
                return "System"
            return actor_names.get(str(actor_id), "System")

        entries: list[TimelineEntry] = []

        for log in audit_logs:
            detail = log.detail or {}
            entries.append(TimelineEntry(timestamp=log.created_at,
                event_type=log.action_type.value,
                actor_name=resolve(log.actor_id),
                description=detail.get("title") or log.action_type.value.replace("_", " ").title(),
            ))

        for h in stage_history:
            from_stage = h.from_stage.value if h.from_stage else "START"
            entries.append(TimelineEntry(timestamp=h.changed_at,
                event_type=f"CANDIDATE_{h.to_stage.value}",
                actor_name=resolve(h.changed_by),
                description=f"Candidate moved {from_stage} → {h.to_stage.value}",
            ))

        for job in bulk_jobs:
            if job.status.value == "COMPLETED":
                job_event = "BULK_UPLOAD_COMPLETED"
                summary = f"Bulk upload '{job.original_filename}' completed — {job.processed_count}/{job.total_files} processed"
            elif job.status.value in ("FAILED", "PARTIAL_FAILURE"):
                job_event = "BULK_UPLOAD_FAILED"
                summary = f"Bulk upload '{job.original_filename}' failed — {job.failed_count}/{job.total_files} failed"
            else:
                job_event = "BULK_UPLOAD_STARTED"
                summary = f"Bulk upload '{job.original_filename}' started — {job.total_files} files"

            entries.append(TimelineEntry(timestamp=job.completed_at or job.created_at,
                event_type=job_event,
                actor_name=resolve(job.uploaded_by),
                description=summary,
            ))

        # computed BEFORE the event_type filter so the dropdown always
        # reflects the campaign's full timeline, not the filtered view
        available_event_types = sorted({e.event_type for e in entries})

        if event_type:
            entries = [e for e in entries if e.event_type == event_type]

        entries.sort(key=lambda e: e.timestamp, reverse=True)

        return CampaignTimelineResponse(campaign_id=campaign_id,
            total_events=len(entries),
            limit=limit,
            offset=offset,
            events=entries[offset: offset + limit],
            available_event_types=available_event_types,
        )

    def _resolve_actor(self, actor_id: str | None) -> str:
        if not actor_id:
            return "System"
        user = self.campaign_repo.get_user(actor_id)
        return user.full_name if user else "System"

    def reset_scoring_to_defaults(self,
        campaign_id: UUID,
        updated_by: str,
    ) -> CampaignScoringConfigurationResponse:
        """
        resets weight_deterministic/semantic/ai and semantic_threshold/
        ai_threshold to the current platform defaults. deterministic_threshold
        is left as-is — the spec's default list doesn't include it. Delegates
        to update_scoring_configuration() so validation, audit logging, and the
        already-processed warning all come from that one implementation rather
        than a second copy of the same rules.
        """
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException(f"Campaign '{campaign_id}' not found", 404)

        configs = self.config_repo.get_configs_by_keys([
                "CAMPAIGN_WEIGHT_DETERMINISTIC",
                "CAMPAIGN_WEIGHT_SEMANTIC",
                "CAMPAIGN_WEIGHT_AI",
                "SEMANTIC_PASS_THRESHOLD",
                "AI_PASS_THRESHOLD",
            ]
        )

        request = CampaignScoringUpdateRequest(weight_deterministic=Decimal(configs.get("CAMPAIGN_WEIGHT_DETERMINISTIC", "30.00")),
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

    def update_platform_default_weights(self,
        request: PlatformDefaultWeightsUpdateRequest,
        updated_by: str,
    ) -> CampaignScoringDefaultsResponse:
        """
        updates the platform_config rows backing get_scoring_configuration's
        "defaults" section and reset_scoring_to_defaults(). Existing campaigns keep
        their own stored weight values untouched — only future defaults/resets change.
        """
        self._validate_scoring_weights(request.weight_deterministic,
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
        if self.cache_service:
            self.cache_service.delete(campaign_platform_defaults_key())

        self.audit_service.log(actor_id=updated_by,
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

        return CampaignScoringDefaultsResponse(weight_deterministic=float(updated["CAMPAIGN_WEIGHT_DETERMINISTIC"]),
            weight_semantic=float(updated["CAMPAIGN_WEIGHT_SEMANTIC"]),
            weight_ai=float(updated["CAMPAIGN_WEIGHT_AI"]),
            semantic_threshold=float(updated["SEMANTIC_PASS_THRESHOLD"]),
            ai_threshold=float(updated["AI_PASS_THRESHOLD"]),
        )


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
            calculate_deterministic_score_task.apply_async(kwargs={"campaign_candidate_id": str(task.campaign_candidate_id)},
                task_id=str(task.task_id),
            )
            task.status = TaskStatus.QUEUED
            requeued += 1
        self.campaign_repo.db.flush()
        return requeued

    def _enqueue_pending_resume_parses(self, campaign_id: UUID, prompt_template_id: UUID) -> int:
        """
        submits a fresh resume.process_document task for every
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

    _SCORING_FIELDS = ("weight_deterministic",
        "weight_semantic",
        "weight_ai",
        "semantic_threshold",
        "ai_threshold",
        "deterministic_threshold",
    )

    def update_campaign(self,
        campaign_id: UUID,
        request: CampaignUpdateRequest,
        updated_by: str,
    ) -> CampaignResponse:
        try:
            # M10-E02: locking read (SELECT ... FOR UPDATE) - same
            # get_by_id_for_update already used by candidate creation's
            # cap-check race guard - so two concurrent PATCHes against the
            # same campaign (in particular two concurrent weight changes)
            # serialize instead of racing to commit.
            campaign = self.campaign_repo.get_by_id_for_update(campaign_id)
            if not campaign:
                raise CampaignException(f"Campaign '{campaign_id}' not found", 404)

            # ── closed campaigns are read-only ──────────────────
            if campaign.status == CampaignStatus.CLOSED:
                # Log the blocked attempt itself (spec requirement), then commit
                # immediately — the raise below triggers this method's own
                # rollback, which would otherwise erase this audit row too.
                self.audit_service.log(actor_id=updated_by,
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

                raise CampaignException("Closed campaigns cannot be edited. Reopen the campaign to make changes.",
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
                    raise CampaignException(f"Unsupported status transition "
                        f"{campaign.status.value} → {request.status.value}.",
                        422,
                    )

            # name / deadline / candidate cap ─────────────────
            if request.name is not None and request.name != campaign.name:
                duplicate = self.campaign_repo.get_by_name(campaign.org_id, request.name)
                if duplicate and duplicate.id != campaign.id:
                    raise CampaignException(f"Campaign name '{request.name}' already exists in this organization",
                        409,
                    )
                changes["name"] = {"before": campaign.name, "after": request.name}
                campaign.name = request.name

            if request.clear_max_candidates:
                if campaign.max_candidates is not None:
                    changes["max_candidates"] = {"before": campaign.max_candidates, "after": None}
                    campaign.max_candidates = None
            elif request.max_candidates is not None and request.max_candidates != campaign.max_candidates:
                # openings can't be cut below the number already filled
                selected_count = self.campaign_repo.get_selected_count(campaign.id)
                if request.max_candidates < selected_count:
                    raise CampaignException(f"Cannot set openings to {request.max_candidates}: the campaign "
                        f"has already selected {selected_count} candidate(s).",
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

            # ── Prompt Template reassignment ─────────────────────────────
            # Pre-existing bug fix (unrelated to M10-E02): the response
            # built at the end of this method reads `updated_prompt`, which
            # was never assigned anywhere - any successful update_campaign
            # call raised NameError before returning. Initialized here (same
            # "default None, set only inside the conditional" pattern
            # already used for previous_hiring_manager_id right below) so
            # the response can report the current prompt name whether or
            # not this PATCH itself changed it.
            updated_prompt = None
            if (request.prompt_template_id is not None
                    and request.prompt_template_id != campaign.prompt_template_id):
                new_prompt = validate_prompt_template_selection(
                    request.prompt_template_id,
                    expected_task_type="RESUME_PARSE",
                    repository=self.prompt_template_repo,
                    exception_factory=lambda msg: CampaignException(msg, 422),
                )
                changes["prompt_template_id"] = {
                    "before": str(campaign.prompt_template_id),
                    "after": str(new_prompt.id),
                }
                campaign.prompt_template_id = new_prompt.id
                updated_prompt = new_prompt

            # ── AI Evaluation Prompt Template reassignment ───────────────
            # Same optional-reassignment shape as prompt_template_id above -
            # ai_evaluate_prompt_id is nullable on HiringCampaign, so a PATCH
            # that omits it (or resends the current value) is a no-op here.
            updated_ai_evaluate_prompt = None
            if (request.ai_evaluate_prompt_id is not None
                    and request.ai_evaluate_prompt_id != campaign.ai_evaluate_prompt_id):
                new_ai_evaluate_prompt = validate_prompt_template_selection(
                    request.ai_evaluate_prompt_id,
                    expected_task_type="AI_EVALUATE",
                    repository=self.prompt_template_repo,
                    exception_factory=lambda msg: CampaignException(msg, 422),
                )
                changes["ai_evaluate_prompt_id"] = {
                    "before": str(campaign.ai_evaluate_prompt_id) if campaign.ai_evaluate_prompt_id else None,
                    "after": str(new_ai_evaluate_prompt.id),
                }
                campaign.ai_evaluate_prompt_id = new_ai_evaluate_prompt.id
                updated_ai_evaluate_prompt = new_ai_evaluate_prompt

            # ── reassign hiring manager ─────────────────────
            previous_hiring_manager_id = None
            hm_review_pending_count = 0
            if (request.hiring_manager_id is not None
                    and request.hiring_manager_id != campaign.hiring_manager_id):
                new_manager = self.campaign_repo.get_user(request.hiring_manager_id)
                if not new_manager:
                    raise CampaignException(f"User '{request.hiring_manager_id}' not found.", 404,
                    )
                if new_manager.role != LocalUserRole.HIRING_MANAGER:
                    raise CampaignException(f"User '{request.hiring_manager_id}' does not have the "
                        f"HIRING_MANAGER role.", 422,
                    )
                if not new_manager.is_active:
                    raise CampaignException(f"User '{request.hiring_manager_id}' is not an active user.", 422,
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

            # ── scoring config gate on ACTIVE campaigns ─────────
            scoring_changes = {
                field: getattr(request, field)
                for field in self._SCORING_FIELDS
                if getattr(request, field) is not None
                and Decimal(str(getattr(campaign, field))) != getattr(request, field)
            }
            # M10-E02: initialized here (before the `if scoring_changes:`
            # guard) since both are read further below regardless of
            # whether that block runs at all - a request with no scoring
            # fields present must never trigger history/audit/recalculation.
            weight_fields_changed: set[str] = set()
            old_weights: dict[str, Decimal] | None = None

            if scoring_changes:
                if campaign.status == CampaignStatus.ACTIVE and not request.confirm_scoring_change:
                    raise CampaignException("Changing scoring configuration will only affect candidates submitted "
                        "after this change. Existing candidate scores will not be recalculated. "
                        "Re-submit with confirm_scoring_change=true to proceed.",
                        422,
                    )

                merged_weights = {
                    field: scoring_changes.get(field, Decimal(str(getattr(campaign, field))))
                    for field in ("weight_deterministic", "weight_semantic", "weight_ai")
                }
                # same validation update_scoring_configuration() runs —
                # sum must equal 100.00 and no layer may fall below MIN_LAYER_WEIGHT,
                # so this endpoint can't be used to bypass either rule.
                self._validate_scoring_weights(**merged_weights)

                # M10-E02: the true "before" weight snapshot, captured BEFORE
                # the setattr loop below mutates `campaign` in place - only
                # taken when at least one weight field is actually changing
                # (as opposed to a thresholds-only scoring_changes), so a
                # no-op/thresholds-only PATCH never triggers history/audit.
                weight_fields_changed = _WEIGHT_FIELDS & scoring_changes.keys()
                if weight_fields_changed:
                    old_weights = {
                        "weight_deterministic": Decimal(str(campaign.weight_deterministic)),
                        "weight_semantic": Decimal(str(campaign.weight_semantic)),
                        "weight_ai": Decimal(str(campaign.weight_ai)),
                    }

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
                # soft-cancel QUEUED tasks (RUNNING finish naturally);
                # uploads are blocked immediately by the PAUSED status guard.
                detail["title"] = f"Campaign '{campaign.name}' paused"
                detail["tasks_suspended"] = self.campaign_repo.suspend_queued_tasks(campaign.id)
                detail["in_flight_bulk_jobs"] = self.campaign_repo.count_processing_bulk_jobs(campaign.id)
                action_type = ActionType.CAMPAIGN_PAUSED
            elif resumed_now:
                # re-queue suspended tasks (PAUSED → QUEUED); uploads are
                # re-permitted immediately by the ACTIVE status.
                detail["title"] = f"Campaign '{campaign.name}' resumed"
                detail["tasks_requeued"] = self._resubmit_paused_tasks(campaign.id)
                detail["resumes_enqueued"] = self._enqueue_pending_resume_parses(campaign.id, campaign.prompt_template_id)

                # pause duration, from the matching CAMPAIGN_PAUSED
                # entry's timestamp to now.
                last_pause = self.audit_service.get_latest_entry(campaign.id, ActionType.CAMPAIGN_PAUSED.value,
                )
                if last_pause is not None:
                    paused_at = last_pause.created_at
                    detail["paused_at"] = paused_at.isoformat()
                    detail["pause_duration_seconds"] = (datetime.now(timezone.utc) - paused_at
                    ).total_seconds()

                action_type = ActionType.CAMPAIGN_RESUMED
            elif scoring_changes:
                action_type = ActionType.CAMPAIGN_SCORING_CONFIG_CHANGED
            else:
                action_type = ActionType.CAMPAIGN_UPDATED

            self.audit_service.log(actor_id=updated_by,
                actor_role="HR_ADMIN",
                action_type=action_type,
                entity_type=EntityType.CAMPAIGN,
                entity_id=campaign.id,
                campaign_id=campaign.id,
                details=detail,
            )

            if previous_hiring_manager_id is not None:
                # dedicated audit entry — always recorded on reassignment,
                # independent of whatever action_type won the main log entry above.
                self.audit_service.log(actor_id=updated_by,
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

            # M10-E02 Story 2: history + dedicated audit entry, only for an
            # actual weight change - never for a thresholds-only change, and
            # never for a no-op resubmission of identical weights (in which
            # case weight_fields_changed is empty and this is skipped
            # entirely, per the No-Op Detection requirement). Written in the
            # same transaction as the campaign update and the audit entry
            # above, before the single commit below.
            if weight_fields_changed:
                self._record_weight_configuration_change(campaign, old_weights, updated_by)

            self.campaign_repo.commit()
            self._invalidate_campaign_caches(campaign.id)

            if weight_fields_changed:
                self._enqueue_composite_recalculation_for_campaign(campaign.id)

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
                prompt_template_id=campaign.prompt_template_id,
                prompt_name=updated_prompt.name if updated_prompt else None,
                ai_evaluate_prompt_id=campaign.ai_evaluate_prompt_id,
                ai_evaluate_prompt_name=updated_ai_evaluate_prompt.name if updated_ai_evaluate_prompt else None,
                candidate_count=candidate_count,
                shortlisted_count=self.campaign_repo.get_shortlisted_count(campaign.id),
                approaching_cap=self._is_approaching_cap(self.campaign_repo.get_selected_count(campaign.id), campaign.max_candidates, cap_warning_percentage),
                deadline_soon=self._is_deadline_soon(campaign.deadline, deadline_warning_days),
            )

            warnings = []
            if hm_review_pending_count > 0:
                # "a specific warning must alert HR_ADMIN that pending HM
                # review decisions may need to be re-communicated to the new manager"
                warnings.append(f"{hm_review_pending_count} candidate(s) are currently awaiting "
                    f"hiring-manager review. These pending decisions may need to be "
                    f"re-communicated to the newly assigned hiring manager."
                )
            if scoring_changes:
                # same warning update_scoring_configuration() shows — must
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

    # ── Pause an Active Campaign ──────────────────────────────────────

    def get_pause_impact_summary(self,
        campaign_id: UUID,
    ) -> PauseImpactSummaryResponse:
        """
        read-only data for the pause confirmation dialog. HR_ADMIN only
        (enforced at the route). Only an ACTIVE campaign can be paused.
        """
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException(f"Campaign '{campaign_id}' not found", 404)

        if campaign.status != CampaignStatus.ACTIVE:
            raise CampaignException("Only an active campaign can be paused.", 409
            )

        return PauseImpactSummaryResponse(candidate_count=self.campaign_repo.get_candidate_count(campaign_id),
            queued_task_count=self.campaign_repo.count_active_queue_tasks(campaign_id),
            processing_bulk_job_count=self.campaign_repo.count_processing_bulk_jobs(campaign_id),
        )

    # ── Resume a Paused Campaign ──────────────────────────────────────

    def get_resume_summary(self,
        campaign_id: UUID,
    ) -> ResumeSummaryResponse:
        """
        read-only data for the resume confirmation dialog. HR_ADMIN only
        (enforced at the route). Only a PAUSED campaign can be resumed.
        """
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException(f"Campaign '{campaign_id}' not found", 404)

        if campaign.status != CampaignStatus.PAUSED:
            raise CampaignException("Only a paused campaign can be resumed.", 409
            )

        paused = self.campaign_repo.count_paused_tasks(campaign_id)
        pending = self.campaign_repo.count_pending_resumes(campaign_id)
        AVG_SECONDS_PER_ITEM = 45  # rough estimate for the "expected load" hint
        total = paused + pending

        return ResumeSummaryResponse(paused_task_count=paused,
            pending_resume_count=pending,
            estimated_processing_seconds=(total * AVG_SECONDS_PER_ITEM) or None,
        )

    # ── Close a Campaign Manually ──────────────────────────────────────

    def get_closure_impact_summary(self,
        campaign_id: UUID,
    ) -> CampaignClosureImpactSummaryResponse:
        """
        read-only data for the close confirmation dialog. HR_ADMIN
        only (enforced at the route). Only an ACTIVE or PAUSED campaign can
        be closed.
        """
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException(f"Campaign '{campaign_id}' not found", 404)

        if campaign.status not in (CampaignStatus.ACTIVE, CampaignStatus.PAUSED):
            raise CampaignException("Only an active or paused campaign can be closed.", 409,
            )

        return CampaignClosureImpactSummaryResponse(candidate_count=self.campaign_repo.get_candidate_count(campaign_id),
            stage_counts=self.campaign_repo.get_stage_counts(campaign_id),
            in_progress_task_count=self.campaign_repo.count_active_queue_tasks(campaign_id),
            pending_human_decision_count=self.campaign_repo.count_pending_human_decision(campaign_id),
            in_progress_bulk_job_count=self.campaign_repo.count_processing_bulk_jobs(campaign_id),
        )

    def close_campaign(self,
        campaign_id: UUID,
        request: CampaignCloseRequest,
        updated_by: str,
    ) -> CampaignClosureResultResponse:
        """
        manual, terminal closure — distinct from the automated
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
                raise CampaignException("Only an active or paused campaign can be closed.", 409,
                )

            tasks_cancelled = self.campaign_repo.kill_queued_tasks(campaign_id)
            bulk_uploads_cancelled = self.campaign_repo.cancel_pending_bulk_jobs(campaign_id)

            self.campaign_repo.close_campaign(campaign)

            stage_counts = self.campaign_repo.get_stage_counts(campaign_id)
            candidate_count = sum(stage_counts.values())

            self.audit_service.log(actor_id=updated_by,
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
            self._invalidate_campaign_caches(campaign.id)

            return CampaignClosureResultResponse(campaign_id=str(campaign.id),
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

    # ── Reopen a Closed Campaign ───────────────────────────────────────

    def get_reopen_readiness(self,
        campaign_id: UUID,
    ) -> CampaignReopenReadinessResponse:
        """
        read-only readiness check for the reopen confirmation dialog.
        HR_ADMIN only (enforced at the route). Only a CLOSED campaign can be
        reopened.
        """
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException(f"Campaign '{campaign_id}' not found", 404)

        if campaign.status != CampaignStatus.CLOSED:
            raise CampaignException("Only a closed campaign can be reopened.", 409,
            )

        jd = self.jd_repo.get_by_id(campaign.jd_id)
        issues: list[JDReadinessIssue] = []

        if not jd:
            issues.append(JDReadinessIssue(code="JD_NOT_FOUND",
                message="The linked job description could not be found.",
            ))
        else:
            if not jd.is_active_version:
                issues.append(JDReadinessIssue(code="JD_NOT_ACTIVE_VERSION",
                    message=f"'{jd.title}' is no longer the active version. Update this campaign to an active JD version before reopening.",
                ))
            if jd.closed_at is not None:
                issues.append(JDReadinessIssue(code="JD_CLOSED",
                    message=f"'{jd.title}' has been closed. Update this campaign to an active, open JD before reopening.",
                ))

            unverified_count = self.campaign_repo.get_mandatory_unverified_skill_count(jd.id)
            if unverified_count > 0:
                issues.append(JDReadinessIssue(code="MANDATORY_SKILLS_UNVERIFIED",
                    message=f"{unverified_count} mandatory skill(s) on '{jd.title}' are still pending verification.",
                ))

            unresolved_count = self.campaign_repo.get_unresolved_unknown_skill_count(jd.id)
            if unresolved_count > 0:
                issues.append(JDReadinessIssue(code="UNRESOLVED_SKILL_EXTRACTION",
                    message=f"{unresolved_count} skill(s) extracted from '{jd.title}' are still unresolved.",
                ))

        return CampaignReopenReadinessResponse(is_ready=not issues,
            issues=issues,
            warnings=self._reopen_cap_warnings(campaign),
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

    def _reopen_cap_warnings(self, campaign) -> list[JDReadinessIssue]:
        """
        A campaign with every opening already filled is worth flagging on
        reopen, but must NOT block it: closed campaigns are read-only, so
        raising the opening count first is impossible and a blocking check
        would strand the campaign closed forever. Reopening is for progressing
        the candidates already in the pipeline.
        """
        if campaign.max_candidates is None:
            return []

        selected_count = self.campaign_repo.get_selected_count(campaign.id)
        if selected_count < campaign.max_candidates:
            return []

        return [JDReadinessIssue(code="ALL_POSITIONS_FILLED",
            message=(f"All {campaign.max_candidates} opening(s) on this campaign are already "
                f"filled ({selected_count} candidate(s) selected). It will reopen, but no "
                f"further candidates can be selected until the opening count is raised "
                f"(editable again once the campaign is ACTIVE)."
            ),
        )]

    def reopen_campaign(self,
        campaign_id: UUID,
        updated_by: str,
    ) -> CampaignReopenResultResponse:
        """
        reopens a closed campaign back to ACTIVE, re-validating
        readiness first. An already-passed deadline is cleared automatically
        (spec: it must be re-set, not silently kept expired). Being at/over the
        candidate cap is reported as a warning, not a blocker — see
        _reopen_cap_warnings().
        """
        try:
            readiness = self.get_reopen_readiness(campaign_id)
            if not readiness.is_ready:
                raise CampaignException("Campaign is not ready to reopen: "
                    + "; ".join(issue.message for issue in readiness.issues),
                    422,
                )

            campaign = self.campaign_repo.get_by_id(campaign_id)

            cap_warnings = self._reopen_cap_warnings(campaign)

            deadline_cleared = False
            if campaign.deadline is not None and campaign.deadline <= datetime.now(timezone.utc):
                campaign.deadline = None
                deadline_cleared = True

            campaign.status = CampaignStatus.ACTIVE
            campaign.updated_at = datetime.now(timezone.utc)
            campaign = self.campaign_repo.update(campaign)

            last_closure = self.audit_service.get_latest_entry(campaign.id, ActionType.CAMPAIGN_CLOSED.value,
            )
            original_closure_reason = None
            closed_at = None
            duration_closed_days = None
            if last_closure is not None:
                closed_at = last_closure.created_at
                original_closure_reason = (last_closure.detail or {}).get("closure_reason")
                duration_closed_days = (datetime.now(timezone.utc) - closed_at
                ).total_seconds() / 86400

            self.audit_service.log(actor_id=updated_by,
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
                    # recorded so an over-cap reopen is traceable even though
                    # it no longer blocks
                    "reopened_at_candidate_cap": bool(cap_warnings),
                },
            )

            self.campaign_repo.commit()
            self._invalidate_campaign_caches(campaign.id)

            return CampaignReopenResultResponse(campaign_id=campaign.id,
                campaign_name=campaign.name,
                status=campaign.status.value,
                reopened_at=campaign.updated_at,
                deadline_cleared=deadline_cleared,
                original_closure_reason=original_closure_reason,
                closed_at=closed_at,
                duration_closed_days=duration_closed_days,
                warning=" ".join(w.message for w in cap_warnings) or None,
            )

        except Exception:
            self.campaign_repo.rollback()
            raise

    def calculate_deterministic_score(self,
        jd_id: UUID,
        resume_id: UUID,
        deterministic_threshold: float,
    ) -> tuple[float, bool]:

        mandatory_skills = (self.skill_repository.get_mandatory_jd_skills(jd_id)
        )

        candidate_skills = (self.skill_repository.get_candidate_normalized_skills(resume_id)
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

        matched_skill_ids = required_skill_ids.intersection(candidate_skill_ids
        )

        score = round((len(matched_skill_ids) / len(required_skill_ids)) * 100,
            2,
        )

        passed = score >= float(deterministic_threshold)

        return score, passed

