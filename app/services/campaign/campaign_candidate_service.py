import hashlib
import logging
from uuid import UUID, uuid4

from datetime import datetime, timedelta, timezone

from fastapi.responses import StreamingResponse

from app.core.encryption_service import DecryptionError, EncryptionService
from app.core.storage_service import StorageService
from app.enums.constants import ActionType, DEFAULT_PAGE_SIZE, EntityType, MAX_PAGE_SIZE
from app.exception_handler.exceptions import NotFoundError
from app.exceptions.campaign_exceptions import CampaignException
from app.exceptions.pipeline_transition_exceptions import (
    InvalidPipelineTransitionException,
    PipelineTransitionReasonRequiredException,
)
from app.models.async_tasks import TaskStatus
from app.models.campaigns import CampaignStatus, HiringCampaign
from app.models.candidates import Candidate, FileFormat, ParseStatus, Resume
from app.models.pipeline import (
    AIEvaluationStatus,
    AIRecommendation,
    CampaignCandidate,
    DecisionSource,
    DecisionType,
    PipelineStage,
    TransitionSource,
)
from app.repositories.allowed_transition_repository import AllowedTransitionRepository
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.campaign_candidate_ai_evaluation_repository import (
    CampaignCandidateAIEvaluationRepository,
)
from app.repositories.campaign_candidate_repository import (
    CampaignCandidateRepository,
)
from app.repositories.candidate_composite_score_history_repository import (
    CandidateCompositeScoreHistoryRepository,
)
from app.repositories.candidate_repository import CandidateRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.resume_repository import ResumeRepository
from app.repositories.skill_repository import SkillRepository
from app.schemas.campaign.campaign_candidate_schema import (
    AiSummaryDetail,
    CampaignCandidateCreateRequest,
    CampaignOverrideAlert,
    CampaignRejectionAnalyticsResponse,
    CandidateAIEvaluationResponse,
    CandidateDeterministicResponse,
    CandidateCampaignHistoryEntryResponse,
    CandidateCampaignHistoryResponse,
    CandidateRejectionHistoryEntryResponse,
    CandidateScorecardResponse,
    CandidateSummaryResponse,
    CampaignCandidateResponse,
    DeterministicScoreBreakdownResponse,
    DeterministicScoreSummary,
    EducationValidationDetail,
    ExperienceValidationDetail,
    HierarchyMatchItem,
    JdCalibrationRecommendation,
    MandatorySkillBreakdownItem,
    MissingMandatorySkillItem,
    MissingSkillOccurrence,
    OverrideReportResponse,
    OverrideReportRow,
    ProcessingTimelineEntry,
    OverrideWeeklyTrendPoint,
    PreferredSkillBreakdownItem,
    RejectionBreakdownEntry,
    ScoreCalculationDetail,
    ScoreConfigurationDetail,
    CampaignCandidateSummaryResponse,
    CandidateCompositeScoreHistoryEntryResponse,
    CandidateCompositeScoreHistoryResponse,
    CandidateRankingDetailsResponse,
    CandidateSemanticResponse,
    CandidateTimelineEventResponse,
    CandidateTimelineResponse,
    RankedCampaignCandidatesResponse,
    SemanticScoreBreakdownResponse,
    SemanticScoreSummary,
    ResubmissionInfoResponse,
    UpdateResumeResubmissionResponse,
)
from app.services.audit_service import AuditService
from app.services.campaign.pipeline_transition_service import PipelineTransitionService
from app.services.campaign.stage_transition_service import StageTransitionService
from app.services.celery_task_log_service import CeleryTaskLogService
from app.tasks.semantic_scoring_tasks import _enqueue_semantic_scoring
from app.services.resume.file_validation_service import FileValidationService
from app.tasks.resume_processing_tasks import process_resume_document
from app.utils.excel_export import ExcelExport

logger = logging.getLogger(__name__)

CANDIDATE_PII_PURPOSE = "CANDIDATE_PII"

# M07-E03 S03 T01: this story's exact, explicit scope - a SEMANTIC/AI-layer
# rejection (a different, not-yet-built epic) never sets has_rejection.
_SCORECARD_BANNER_DECISION_SOURCE = DecisionSource.DETERMINISTIC

# Epic 3 (M05-E03) Phase C5 - same bucket/extension mapping
# ResumeUploadService.upload() uses; duplicated here (not imported, since
# both are module-private to their own file) rather than introducing
# cross-service coupling for four lines.
_RESUME_STORAGE_BUCKET = "airs_resumes"
_RESUBMISSION_FORMAT_TO_EXTENSION = {
    FileFormat.PDF: "pdf",
    FileFormat.DOCX: "docx",
}

# M07-E03 S04 T02: task_type strings this service queues after an override.
# Neither has a real Celery task implementation anywhere in this codebase
# yet (M09 AI Evaluation / semantic scoring aren't built) - mirrors the
# exact same forward-compatible placeholder already established by
# deterministic_scoring_tasks.py's AI_EVALUATE_TASK_TYPE/
# _cancel_downstream_ai_evaluation (M07-E03 S01 T03): "queuing" is recorded
# as a QUEUED celery_task_log row, which the real tasks will activate
# against once built, without requiring any further change here. Must
# match deterministic_scoring_tasks.AI_EVALUATE_TASK_TYPE exactly.
AI_EVALUATE_TASK_TYPE = "AI_EVALUATE"
SEMANTIC_SCORE_TASK_TYPE = "SEMANTIC_SCORE"

_HR_OVERRIDE_CHANGE_REASON = "HR_ADMIN override of deterministic rejection"
_OVERRIDE_RATE_ALERT_THRESHOLD_KEY = "OVERRIDE_RATE_ALERT_THRESHOLD"
_DEFAULT_OVERRIDE_RATE_ALERT_THRESHOLD = 20.0
_OVERRIDE_RECOMMENDATION = "Review campaign JD skills or thresholds."
_WEEKLY_TREND_WEEKS = 8

# M07-E03 S05: Deterministic Rejection Analytics
_MIN_CANDIDATES_FOR_ANALYTICS_KEY = "MIN_CANDIDATES_FOR_ANALYTICS"
_DEFAULT_MIN_CANDIDATES_FOR_ANALYTICS = 20
# Reuses the exact same platform_config key deterministic_scoring_tasks.py
# already reads for scoring (never a second, S05-local key).
_EXPERIENCE_TOLERANCE_YEARS_KEY = "EXPERIENCE_TOLERANCE_YEARS"

# The 7 mandatory-skill/experience/education failure-combination buckets,
# in the exact order this story lists them.
_SKILLS_ONLY = "SKILLS_ONLY"
_EXPERIENCE_ONLY = "EXPERIENCE_ONLY"
_EDUCATION_ONLY = "EDUCATION_ONLY"
_SKILLS_EXPERIENCE = "SKILLS_EXPERIENCE"
_SKILLS_EDUCATION = "SKILLS_EDUCATION"
_EXPERIENCE_EDUCATION = "EXPERIENCE_EDUCATION"
_SKILLS_EXPERIENCE_EDUCATION = "SKILLS_EXPERIENCE_EDUCATION"

_BREAKDOWN_CATEGORY_ORDER = [
    _SKILLS_ONLY,
    _EXPERIENCE_ONLY,
    _EDUCATION_ONLY,
    _SKILLS_EXPERIENCE,
    _SKILLS_EDUCATION,
    _EXPERIENCE_EDUCATION,
    _SKILLS_EXPERIENCE_EDUCATION,
]

_BREAKDOWN_CATEGORY_DISPLAY = {
    _SKILLS_ONLY: "Missing Skills Only",
    _EXPERIENCE_ONLY: "Experience Only",
    _EDUCATION_ONLY: "Education Only",
    _SKILLS_EXPERIENCE: "Skills + Experience",
    _SKILLS_EDUCATION: "Skills + Education",
    _EXPERIENCE_EDUCATION: "Experience + Education",
    _SKILLS_EXPERIENCE_EDUCATION: "Skills + Experience + Education",
}

# T02 Rule 1/2 thresholds - "do not hardcode thresholds, always read
# PlatformConfig" applies here too, not only to OVERRIDE_RATE_ALERT_THRESHOLD -
# these keys are new (S05), seeded with the ticket's own stated percentages
# as the default value; the numeric constants below are ONLY the in-code
# fallback used if PlatformConfig is unreachable, never the value actually
# compared against.
_SKILL_MISMATCH_RATE_THRESHOLD_KEY = "SKILL_MISMATCH_RATE_THRESHOLD"
_DEFAULT_SKILL_MISMATCH_RATE_THRESHOLD = 60.0
_EXPERIENCE_ONLY_RATE_THRESHOLD_KEY = "EXPERIENCE_ONLY_RATE_THRESHOLD"
_DEFAULT_EXPERIENCE_ONLY_RATE_THRESHOLD = 40.0

# Deterministic Score API response contract: display-only config keys for
# DeterministicScoreBreakdownResponse.configuration - reuses the exact same
# platform_config keys deterministic_scoring_tasks.py/candidate_scoring_service.py
# already read for scoring, never a second/duplicated key. Read-only lookups
# for display; never written here, never used to recompute anything.
_DETERMINISTIC_WEIGHT_SKILLS_KEY = "DETERMINISTIC_WEIGHT_SKILLS"
_DETERMINISTIC_WEIGHT_EXPERIENCE_KEY = "DETERMINISTIC_WEIGHT_EXPERIENCE"
_DETERMINISTIC_WEIGHT_EDUCATION_KEY = "DETERMINISTIC_WEIGHT_EDUCATION"
_HIERARCHY_SEMANTIC_ONLY_THRESHOLD_KEY = "HIERARCHY_SEMANTIC_ONLY_THRESHOLD"
_HIERARCHY_GRANDCHILD_MULTIPLIER_KEY = "HIERARCHY_GRANDCHILD_MULTIPLIER"
# Not seeded/present in PlatformConfig today (CHILD=0.7/SIBLING=0.4/
# SEMANTIC=0.2 are hardcoded literals in CandidateScoringService) - reading
# these keys anyway is forward-compatible and always resolves to None
# under the current system, never a DB/seed change made here.
_HIERARCHY_CHILD_MULTIPLIER_KEY = "HIERARCHY_CHILD_MULTIPLIER"
_HIERARCHY_SIBLING_MULTIPLIER_KEY = "HIERARCHY_SIBLING_MULTIPLIER"
_SEMANTIC_MULTIPLIER_KEY = "SEMANTIC_MULTIPLIER"

# Match-type values that count as a hierarchy fallback (not EXACT, not MISSING).
_HIERARCHY_RELATIONSHIP_MATCH_TYPES = {"CHILD", "GRANDCHILD", "SIBLING", "SEMANTIC"}

_SKILL_MATCH_TIER_LABELS = {
    "CHILD": "a related child skill",
    "GRANDCHILD": "a related grandchild skill",
    "SIBLING": "a related sibling skill",
    "SEMANTIC": "a semantically similar skill",
}


class CampaignCandidateService:

    def __init__(
        self,
        campaign_repo: CampaignRepository,
        campaign_candidate_repo: CampaignCandidateRepository,
        audit_service: AuditService,
        encryption_service: EncryptionService | None = None,
        candidate_repo: CandidateRepository | None = None,
        resume_repo: ResumeRepository | None = None,
        ai_evaluation_repo: CampaignCandidateAIEvaluationRepository | None = None,
        stage_transition_service: StageTransitionService | None = None,
        config_repo: ConfigRepository | None = None,
        celery_task_log_service: CeleryTaskLogService | None = None,
        skill_repo: SkillRepository | None = None,
        allowed_transition_repo: AllowedTransitionRepository | None = None,
        pipeline_transition_service: PipelineTransitionService | None = None,
        file_validation_service: FileValidationService | None = None,
        storage_service: StorageService | None = None,
        composite_score_history_repo: CandidateCompositeScoreHistoryRepository | None = None,
    ):
        self.campaign_repo = campaign_repo
        self.campaign_candidate_repo = campaign_candidate_repo
        self.audit_service = audit_service
        # M10-E03 Phase 2 — optional, additive, same convention as every
        # other optional dep here: every pre-existing call site is
        # unaffected. This service has no direct `db` handle of its own (it
        # is pure repository composition), so the fallback derives its
        # session from campaign_candidate_repo - the one repository every
        # caller is already required to construct with the correct
        # request-scoped session - rather than adding a new `db` parameter.
        self.composite_score_history_repo = (
            composite_score_history_repo
            or CandidateCompositeScoreHistoryRepository(campaign_candidate_repo.db)
        )
        # Epic 3 (M05-E03) Phase C5 — optional, additive, same convention as
        # the other optional deps above: every pre-existing call site
        # (including bulk_upload_tasks.py's own direct construction) never
        # passes these, so their behavior is completely unchanged; only the
        # new resubmission-info enrichment and update_resume_for_resubmission
        # need them.
        self.allowed_transition_repo = allowed_transition_repo
        self.pipeline_transition_service = pipeline_transition_service
        self.file_validation_service = file_validation_service
        self.storage_service = storage_service
        self.encryption_service = encryption_service
        # M07-E03 S03: optional, additive - every pre-existing call site
        # (create/list/delete) never passes these, so their behavior is
        # completely unchanged.
        self.candidate_repo = candidate_repo
        self.resume_repo = resume_repo
        self.ai_evaluation_repo = ai_evaluation_repo or CampaignCandidateAIEvaluationRepository(
            campaign_candidate_repo.db,
        )
        # M07-E03 S04: optional, additive - same reasoning as above.
        self.stage_transition_service = stage_transition_service
        self.config_repo = config_repo
        self.celery_task_log_service = celery_task_log_service
        # M07-E03 S05: optional, additive - same reasoning as above.
        self.skill_repo = skill_repo

    def check_no_existing_campaign_membership(
        self,
        campaign_id: UUID,
        candidate_email: str,
    ) -> None:
        """
        Fast, non-authoritative pre-check for ResumeIntakeService.upload_resume
        to call BEFORE any file upload / candidate / resume creation happens —
        create_campaign_candidate's own identical check further down only
        runs after all of that has already been committed, so a rejection
        there used to leave an orphaned Resume row behind on every "already
        in this campaign" upload attempt. Not a substitute for that
        locked, race-safe check, which remains the source of truth for the
        actual insert.
        """
        if self.encryption_service is None or self.candidate_repo is None:
            return

        email_hash = self.encryption_service.generate_hash(candidate_email)
        candidate = self.candidate_repo.get_by_email_hash(email_hash)
        if candidate is None:
            return

        existing = self.campaign_candidate_repo.get_by_campaign_and_candidate(campaign_id, candidate.id)
        if existing:
            raise CampaignException(
                "Candidate already exists in this campaign.",
                409,
                data=self._build_resubmission_info(existing),
            )

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
                    data=self._build_resubmission_info(existing_candidate),
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

            # No cap check on intake: max_candidates counts openings, which are
            # consumed when a candidate reaches SELECTED (enforced in
            # PipelineTransitionService), not when a resume is added.

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

    def _build_resubmission_info(
        self,
        campaign_candidate: CampaignCandidate,
    ) -> ResubmissionInfoResponse | None:
        """
        Epic 3 (M05-E03) Phase C5 — resolves whether an "update resume"
        resubmission is currently possible for this campaign_candidate, by
        checking the real allowed_transitions data rather than duplicating
        the seeded graph as a hardcoded assumption here. Returns None (the
        exception's data stays None, exactly like before this phase) when
        allowed_transition_repo isn't wired — e.g. bulk_upload_tasks.py's
        own direct CampaignCandidateService(...) construction, which never
        passes it and doesn't need this enrichment.
        """
        if self.allowed_transition_repo is None:
            return None

        transition = self.allowed_transition_repo.get(
            campaign_candidate.pipeline_stage, PipelineStage.UPLOADED,
        )
        can_update_resume = transition is not None
        requires_hr_confirmation = can_update_resume and set(transition.allowed_roles) == {"HR_ADMIN"}

        return ResubmissionInfoResponse(
            campaign_candidate_id=campaign_candidate.id,
            candidate_id=campaign_candidate.candidate_id,
            current_pipeline_stage=campaign_candidate.pipeline_stage,
            current_resume_id=campaign_candidate.resume_id,
            can_update_resume=can_update_resume,
            requires_hr_confirmation=requires_hr_confirmation,
        )

    def update_resume_for_resubmission(
        self,
        campaign_candidate_id: UUID,
        file_bytes: bytes,
        filename: str,
        actor_id: str,
        actor_role: str | None = None,
        reason: str | None = None,
        content_type: str | None = None,
    ) -> UpdateResumeResubmissionResponse:
        """
        Epic 3 (M05-E03) Phase C5 — the "update resume" resolution action:
        moves pipeline_stage back to UPLOADED (validated + audited by
        PipelineTransitionService, C0), creates the next resume version
        under the same candidate (C1's versioning), resets every
        evaluation-derived field, and re-enqueues RESUME_PARSE. Requires
        allowed_transition_repo/pipeline_transition_service/
        file_validation_service/storage_service/resume_repo all be wired -
        true for the real DI-constructed service (see
        get_campaign_candidate_service), not for bulk_upload_tasks.py's own
        narrower construction, which never calls this method.
        """
        campaign_candidate = self.campaign_candidate_repo.get_by_id(campaign_candidate_id)
        if campaign_candidate is None:
            raise CampaignException("Campaign candidate not found.", 404)

        try:
            self.pipeline_transition_service.transition_stage(
                campaign_candidate,
                to_stage=PipelineStage.UPLOADED,
                changed_by=actor_id,
                actor_role=actor_role,
                reason=reason,
                source=TransitionSource.MANUAL,
            )
        except InvalidPipelineTransitionException as exc:
            self.campaign_candidate_repo.rollback()
            raise CampaignException(str(exc), 409) from exc
        except PipelineTransitionReasonRequiredException as exc:
            self.campaign_candidate_repo.rollback()
            raise CampaignException(str(exc), 400) from exc

        try:
            validation_result = self.file_validation_service.validate(file_bytes, filename)

            extension = _RESUBMISSION_FORMAT_TO_EXTENSION[validation_result.file_format]
            object_path = f"org_None/resume/{uuid4()}.{extension}"
            self.storage_service.upload_file(
                bucket_name=_RESUME_STORAGE_BUCKET,
                file_path=object_path,
                file_content=file_bytes,
                content_type=content_type,
            )

            file_hash = hashlib.md5(file_bytes).hexdigest()

            existing_active = self.resume_repo.get_active_by_candidate(campaign_candidate.candidate_id)
            if existing_active is None:
                version_number = 1
            else:
                version_number = self.resume_repo.get_max_version_number(campaign_candidate.candidate_id) + 1
                self.resume_repo.deactivate_active_version(campaign_candidate.candidate_id)

            new_resume = Resume(
                candidate_id=campaign_candidate.candidate_id,
                file_path=object_path,
                file_format=validation_result.file_format,
                file_hash=file_hash,
                version_number=version_number,
                is_active_version=True,
                parse_status=ParseStatus.PENDING,
                uploaded_by=actor_id,
            )
            new_resume = self.resume_repo.create(new_resume)

            self.campaign_candidate_repo.reset_for_resubmission(campaign_candidate, new_resume.id)
            ai_evaluation = self.ai_evaluation_repo.get_by_campaign_candidate_id(campaign_candidate.id)
            if ai_evaluation is not None:
                self.ai_evaluation_repo.reset(ai_evaluation)
            self.campaign_candidate_repo.commit()
        except Exception:
            self.campaign_candidate_repo.rollback()
            raise

        campaign = self.campaign_repo.get_by_id(campaign_candidate.campaign_id)

        task_id = uuid4()
        self.resume_repo.set_task_id(new_resume, str(task_id))
        self.resume_repo.commit()
        process_resume_document.apply_async(
            kwargs={"resume_id": str(new_resume.id), "prompt_template_id": str(campaign.prompt_template_id)},
            task_id=str(task_id),
        )

        return UpdateResumeResubmissionResponse(
            campaign_candidate=CampaignCandidateResponse.model_validate(campaign_candidate),
            new_resume_id=new_resume.id,
            task_id=task_id,
        )

    def get_candidate_campaign_history(self, candidate_id: UUID) -> CandidateCampaignHistoryResponse:
        """
        Epic 3 (M05-E03) Phase C6 — HR_ADMIN-only cross-campaign history.
        Read-only; every score/stage field already lives on the per-campaign
        campaign_candidates row, so there is no cross-campaign contamination
        risk to guard against here (verified separately via test, not code).
        """
        rows = self.campaign_candidate_repo.get_all_by_candidate_across_campaigns(candidate_id)
        if not rows:
            raise NotFoundError(f"No campaign history found for candidate {candidate_id}.")

        history = [
            CandidateCampaignHistoryEntryResponse(
                campaign_candidate_id=campaign_candidate.id,
                campaign_id=campaign_candidate.campaign_id,
                campaign_name=campaign_name,
                jd_title=jd_title,
                submission_date=campaign_candidate.created_at,
                pipeline_stage=campaign_candidate.pipeline_stage,
                composite_score=campaign_candidate.composite_score,
                outcome=self._derive_outcome(campaign_candidate.pipeline_stage),
            )
            for campaign_candidate, campaign_name, jd_title in rows
        ]

        return CandidateCampaignHistoryResponse(
            candidate_id=candidate_id,
            total_campaigns=len(history),
            history=history,
        )

    @staticmethod
    def _derive_outcome(pipeline_stage: PipelineStage) -> str:
        if pipeline_stage == PipelineStage.SELECTED:
            return "Selected"
        if pipeline_stage == PipelineStage.REJECTED:
            return "Rejected"
        return "In Progress"

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

    def get_ranked_campaign_candidates(
        self,
        campaign_id: UUID,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
        sort_by: str | None = None,
        sort_order: str = "desc",
        pipeline_stage: PipelineStage | None = None,
        composite_score_min: float | None = None,
        composite_score_max: float | None = None,
        ai_recommendation: AIRecommendation | None = None,
        ai_evaluation_status: AIEvaluationStatus | None = None,
        include_pending: bool = True,
        include_rejected: bool = True,
        include_fraud: bool = True,
        hr_override: bool | None = None,
    ) -> RankedCampaignCandidatesResponse:
        """
        M10-E03 Phase 1: the ranked, filtered, paginated candidate list -
        the extended shape of get_campaign_candidates(), which remains
        unchanged above for any existing direct caller. All filtering and
        ordering happens in
        CampaignCandidateRepository.get_ranked_by_campaign() (PostgreSQL,
        never Python); this method only validates, delegates, and maps
        rows to response DTOs with their 1-based rank within the current
        page. Read-only - never writes an audit entry, matching every
        other read-only listing/detail endpoint in this service.
        """
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException("Campaign not found.", 404)

        if page < 1:
            raise CampaignException("page must be >= 1.", 422)
        if page_size < 1 or page_size > MAX_PAGE_SIZE:
            raise CampaignException(f"page_size must be between 1 and {MAX_PAGE_SIZE}.", 422)
        if (
            composite_score_min is not None
            and composite_score_max is not None
            and composite_score_min > composite_score_max
        ):
            raise CampaignException("composite_score_min must not be greater than composite_score_max.", 422)

        rows, total = self.campaign_candidate_repo.get_ranked_by_campaign(
            campaign_id,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
            pipeline_stage=pipeline_stage,
            composite_score_min=composite_score_min,
            composite_score_max=composite_score_max,
            ai_recommendation=ai_recommendation,
            ai_evaluation_status=ai_evaluation_status,
            include_pending=include_pending,
            include_rejected=include_rejected,
            include_fraud=include_fraud,
            hr_override=hr_override,
        )

        first_rank_on_page = (page - 1) * page_size + 1
        items = [
            self._to_campaign_candidate_response(campaign_candidate, candidate, resume, rank=first_rank_on_page + offset)
            for offset, (campaign_candidate, candidate, resume) in enumerate(rows)
        ]

        return RankedCampaignCandidatesResponse(items=items, page=page, page_size=page_size, total=total)

    def get_campaign_candidate_summary(self, campaign_id: UUID) -> CampaignCandidateSummaryResponse:
        """
        M10-E03 Phase 1: aggregate ranking statistics for one campaign -
        total/ranked/pending/rejected/fraud counts, composite_score
        highest/lowest/average, and pipeline-stage + AI-recommendation
        breakdowns. Reuses CampaignRepository.get_stage_counts() (M07-E01)
        as-is for the pipeline-stage breakdown - and derives
        rejected_candidates from that same result - rather than running a
        second, duplicate REJECTED-count query.  Read-only - never writes
        an audit entry.
        """
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException("Campaign not found.", 404)

        stage_counts = self.campaign_repo.get_stage_counts(campaign_id)
        aggregates = self.campaign_candidate_repo.get_score_aggregates(campaign_id)
        ai_recommendation_counts = self.campaign_candidate_repo.get_ai_recommendation_counts(campaign_id)

        pending = aggregates["total"] - aggregates["ranked"] - aggregates["failed"]

        return CampaignCandidateSummaryResponse(
            total_candidates=aggregates["total"],
            ranked_candidates=aggregates["ranked"],
            pending_candidates=pending,
            rejected_candidates=stage_counts.get(PipelineStage.REJECTED.value, 0),
            fraud_candidates=aggregates["fraud"],
            highest_composite_score=aggregates["highest"],
            lowest_composite_score=aggregates["lowest"],
            average_composite_score=(
                round(aggregates["average"], 2) if aggregates["average"] is not None else None
            ),
            pipeline_stage_counts=stage_counts,
            ai_recommendation_counts=ai_recommendation_counts,
        )

    @staticmethod
    def _derive_ranking_status(campaign_candidate: CampaignCandidate) -> str:
        """
        M10-E03 Phase 1: RANKED/PENDING/FAILED, derived on every read from
        already-loaded columns - never stored. RANKED whenever
        composite_score is present, regardless of pipeline_stage (a
        rejected candidate that was scored before rejection keeps its
        RANKED status - rejection is surfaced separately via
        pipeline_stage/include_rejected, not by hiding the score). Absent a
        score: FAILED only when ai_evaluation_status is explicitly FAILED
        (a genuine pipeline error); every other no-score case (not yet
        reached AI evaluation, still IN_PROGRESS, SKIPPED, MANUAL_REVIEW,
        or rejected at an earlier layer before AI evaluation ever ran) is
        PENDING.
        """
        if campaign_candidate.composite_score is not None:
            return "RANKED"
        # getattr-guarded: real CampaignCandidate rows always have this
        # relationship; some pre-M10-E03 test fixtures build bare
        # SimpleNamespace objects that predate it - defaulting to None
        # (-> PENDING) keeps those fixtures passing unchanged rather than
        # requiring every one of them to be updated for an unrelated field.
        # ai_evaluation_status now lives on the related
        # CampaignCandidateAIEvaluation row (1:1) - a candidate with no
        # such row yet has no status, same as its pre-split PENDING default.
        ai_evaluation = getattr(campaign_candidate, "ai_evaluation", None)
        ai_evaluation_status = getattr(ai_evaluation, "ai_evaluation_status", None) if ai_evaluation else None
        if ai_evaluation_status == AIEvaluationStatus.FAILED:
            return "FAILED"
        return "PENDING"

    @staticmethod
    def _ai_evaluation(campaign_candidate: CampaignCandidate):
        """
        AI evaluation fields moved off CampaignCandidate onto the related
        CampaignCandidateAIEvaluation row (1:1) - every read site guards for
        None the same way (no row yet -> every AI field reads as if still
        at its pre-split default). getattr-guarded for the same
        pre-M10-E03 SimpleNamespace test fixtures noted elsewhere in this
        file.
        """
        return getattr(campaign_candidate, "ai_evaluation", None)

    def get_candidate_timeline(self, campaign_candidate_id: UUID) -> CandidateTimelineResponse:
        """
        M10-E03 Phase 2 Story 1: the complete Candidate Stage Timeline -
        reuses the existing campaign_candidate_stage_history table as-is
        (no new history table, no duplicated stage tracking). Read-only:
        never writes a stage-history row, never writes an audit entry.
        Every event is returned exactly as stored - from_stage/to_stage/
        transition_source use the existing PipelineStage/TransitionSource
        enums verbatim, never a new value set.
        """
        campaign_candidate = self.campaign_candidate_repo.get_by_id(campaign_candidate_id)
        if not campaign_candidate:
            raise CampaignException("Campaign candidate not found.", 404)

        history_rows = self.campaign_candidate_repo.get_stage_history_by_campaign_candidate_id(
            campaign_candidate_id,
        )

        events = [
            CandidateTimelineEventResponse(
                from_stage=row.from_stage,
                to_stage=row.to_stage,
                transition_source=row.transition_source,
                changed_by=row.changed_by,
                changed_at=row.changed_at,
                comments=row.change_reason,
                metadata=row.scores_snapshot,
            )
            for row in history_rows
        ]

        return CandidateTimelineResponse(
            campaign_candidate_id=campaign_candidate.id,
            current_stage=campaign_candidate.pipeline_stage,
            events=events,
        )

    def get_candidate_composite_history(self, campaign_candidate_id: UUID) -> CandidateCompositeScoreHistoryResponse:
        """
        M10-E03 Phase 2 Story 2: the complete, immutable
        candidate_composite_score_history trail (Epic 1) for one candidate,
        most recent first - exactly as CompositeScoringService originally
        persisted it. Never recalculates, never mutates - the repository
        this delegates to (CandidateCompositeScoreHistoryRepository) has no
        update()/delete() method at all, so there is no code path here that
        could touch this data.
        """
        campaign_candidate = self.campaign_candidate_repo.get_by_id(campaign_candidate_id)
        if not campaign_candidate:
            raise CampaignException("Campaign candidate not found.", 404)

        history_rows = self.composite_score_history_repo.get_by_campaign_candidate_id(campaign_candidate_id)

        entries = [
            CandidateCompositeScoreHistoryEntryResponse(
                calculated_at=row.calculated_at,
                trigger_source=row.trigger_source,
                formula_version=row.formula_version,
                weight_deterministic=float(row.weight_deterministic),
                weight_semantic=float(row.weight_semantic),
                weight_ai=float(row.weight_ai),
                deterministic_score=float(row.deterministic_score) if row.deterministic_score is not None else None,
                semantic_score=float(row.semantic_score) if row.semantic_score is not None else None,
                normalized_semantic_score=(
                    float(row.normalized_semantic_score) if row.normalized_semantic_score is not None else None
                ),
                effective_ai_score=float(row.effective_ai_score) if row.effective_ai_score is not None else None,
                composite_score=float(row.composite_score),
            )
            for row in history_rows
        ]

        return CandidateCompositeScoreHistoryResponse(
            campaign_candidate_id=campaign_candidate.id,
            entries=entries,
        )

    def get_candidate_ranking_details(self, campaign_candidate_id: UUID) -> CandidateRankingDetailsResponse:
        """
        M10-E03 Phase 2 Story 3: "why does this candidate currently have
        this ranking" - a read-only aggregation of already-stored fields.
        Never calls CompositeScoringService and never recomputes anything;
        `weight_*` are the campaign's CURRENT weights (which may have
        changed since composite_score was last calculated - the weights
        actually in effect at calculation time are on the Composite History
        API instead, not duplicated here). `formula_version` is read from
        the candidate's most recent candidate_composite_score_history row
        (reusing the same repository call Story 2 uses, not a second/
        independent query) - None when composite_score has never been
        calculated. ranking_status reuses _derive_ranking_status verbatim -
        no second status concept.
        """
        campaign_candidate = self.campaign_candidate_repo.get_by_id(campaign_candidate_id)
        if not campaign_candidate:
            raise CampaignException("Campaign candidate not found.", 404)

        campaign = self.campaign_repo.get_by_id(campaign_candidate.campaign_id)
        if not campaign:
            raise CampaignException("Campaign not found.", 404)

        history_rows = self.composite_score_history_repo.get_by_campaign_candidate_id(campaign_candidate_id)
        formula_version = history_rows[0].formula_version if history_rows else None
        ai_evaluation = self._ai_evaluation(campaign_candidate)
        is_overridden = campaign_candidate.decision_type == DecisionType.RESET

        return CandidateRankingDetailsResponse(
            campaign_candidate_id=campaign_candidate.id,
            composite_score=(
                float(campaign_candidate.composite_score) if campaign_candidate.composite_score is not None else None
            ),
            deterministic_score=(
                float(campaign_candidate.deterministic_score)
                if campaign_candidate.deterministic_score is not None else None
            ),
            semantic_score=(
                float(campaign_candidate.semantic_score) if campaign_candidate.semantic_score is not None else None
            ),
            ai_evaluation_score=(
                float(ai_evaluation.effective_ai_score)
                if ai_evaluation and ai_evaluation.effective_ai_score is not None else None
            ),
            weight_deterministic=float(campaign.weight_deterministic),
            weight_semantic=float(campaign.weight_semantic),
            weight_ai=float(campaign.weight_ai),
            formula_version=formula_version,
            ranking_status=self._derive_ranking_status(campaign_candidate),
            composite_score_computed_at=getattr(campaign_candidate, "composite_score_computed_at", None),
            hr_override=is_overridden,
            hr_override_by=campaign_candidate.decision_by_user_id if is_overridden else None,
            hr_override_reason=campaign_candidate.decision_reason if is_overridden else None,
            hr_override_at=campaign_candidate.decision_at if is_overridden else None,
        )

    def _to_campaign_candidate_response(
        self,
        campaign_candidate: CampaignCandidate,
        candidate: Candidate | None,
        resume: Resume | None,
        rank: int | None = None,
    ) -> CampaignCandidateResponse:
        designation, experience = self._extract_designation_and_experience(resume)
        ai_evaluation = self._ai_evaluation(campaign_candidate)

        return CampaignCandidateResponse(
            id=campaign_candidate.id,
            campaign_id=campaign_candidate.campaign_id,
            candidate_id=campaign_candidate.candidate_id,
            campaign_candidate_id=campaign_candidate.id,
            resume_id=campaign_candidate.resume_id,
            pipeline_stage=campaign_candidate.pipeline_stage,
            parse_status=resume.parse_status if resume else None,
            candidate_name=self._decrypt_candidate_name(candidate),
            current_designation=designation,
            experience=experience,
            deterministic_score=(
                float(campaign_candidate.deterministic_score)
                if campaign_candidate.deterministic_score is not None else None
            ),
            ai_ats_score=(
                float(ai_evaluation.ai_ats_score)
                if ai_evaluation and ai_evaluation.ai_ats_score is not None else None
            ),
            semantic_score=(
                float(campaign_candidate.semantic_score)
                if campaign_candidate.semantic_score is not None else None
            ),
            composite_score=(
                float(campaign_candidate.composite_score)
                if campaign_candidate.composite_score is not None else None
            ),
            # getattr-guarded (see _derive_ranking_status's docstring note) -
            # real CampaignCandidate rows always have these columns; a few
            # pre-M10-E03 test fixtures build bare SimpleNamespace objects
            # that predate them.
            is_fraud_flagged=getattr(campaign_candidate, "is_fraud_flagged", False),
            hr_override=getattr(campaign_candidate, "decision_type", None) == DecisionType.RESET,
            ai_recommendation=ai_evaluation.ai_recommendation if ai_evaluation else None,
            rank=rank,
            ranking_status=self._derive_ranking_status(campaign_candidate),
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

    # ------------------------------------------------------------------
    # M07-E03 S03 T01: Candidate Scorecard rejection banner
    # ------------------------------------------------------------------

    def get_campaign_candidate_scorecard(
        self,
        campaign_candidate_id: UUID,
    ) -> CandidateScorecardResponse:
        """
        Single-candidate detail view - extends the exact same fields
        get_campaign_candidates already returns, plus the rejection
        banner. Nothing here is recalculated: score_breakdown and the
        rejection fields are read exactly as already stored/computed by
        the deterministic scoring task (M07-E01/E02, M07-E03 S01).
        """
        campaign_candidate = self.campaign_candidate_repo.get_by_id(campaign_candidate_id)
        if not campaign_candidate:
            raise CampaignException("Campaign candidate not found.", 404)

        candidate = (
            self.candidate_repo.get_by_id(campaign_candidate.candidate_id)
            if self.candidate_repo is not None else None
        )
        resume = (
            self.resume_repo.get_by_id(campaign_candidate.resume_id)
            if self.resume_repo is not None else None
        )
        base = self._to_campaign_candidate_response(campaign_candidate, candidate, resume)
        banner = self._build_rejection_banner(campaign_candidate)
        deterministic_score_breakdown = self._build_deterministic_score_breakdown(
            campaign_candidate, rejection_reason=banner.get("rejection_reason"),
        )

        return CandidateScorecardResponse(
            **base.model_dump(), **banner,
            deterministic_score_breakdown=deterministic_score_breakdown,
        )

    # ------------------------------------------------------------------
    # Candidate Scorecard tab endpoints: Summary / Deterministic.
    # Each reuses the exact same shared helpers get_campaign_candidate_scorecard
    # itself uses (_to_campaign_candidate_response / _build_rejection_banner /
    # _build_deterministic_score_breakdown) - no second/independent
    # computation, no business-logic duplication. The full aggregate
    # endpoint above is untouched and stays fully backward compatible.
    # ------------------------------------------------------------------

    def get_candidate_summary(self, campaign_candidate_id: UUID) -> CandidateSummaryResponse:
        """
        Summary-tab-only view: header, candidate info, overall scores, AI
        summary (if available). Never includes score_breakdown/
        deterministic_score_breakdown (Deterministic tab) or the rejection/
        override banner (future Final Status tab) - those live in their own
        dedicated responses.
        """
        campaign_candidate = self.campaign_candidate_repo.get_by_id(campaign_candidate_id)
        if not campaign_candidate:
            raise CampaignException("Campaign candidate not found.", 404)

        candidate = (
            self.candidate_repo.get_by_id(campaign_candidate.candidate_id)
            if self.candidate_repo is not None else None
        )
        resume = (
            self.resume_repo.get_by_id(campaign_candidate.resume_id)
            if self.resume_repo is not None else None
        )
        # Reused as-is - the exact same mapper get_campaign_candidate_scorecard
        # calls for its own base fields.
        base = self._to_campaign_candidate_response(campaign_candidate, candidate, resume)

        return CandidateSummaryResponse(
            campaign_candidate_id=base.campaign_candidate_id,
            campaign_id=base.campaign_id,
            candidate_id=base.candidate_id,
            pipeline_stage=base.pipeline_stage,
            created_at=base.created_at,
            candidate_name=base.candidate_name,
            current_designation=base.current_designation,
            experience=base.experience,
            location=base.location,
            deterministic_score=base.deterministic_score,
            ai_ats_score=base.ai_ats_score,
            semantic_score=base.semantic_score,
            composite_score=base.composite_score,
            ai_summary=self._build_ai_summary(campaign_candidate),
        )

    @staticmethod
    def _build_ai_summary(campaign_candidate: CampaignCandidate) -> AiSummaryDetail | None:
        ai_evaluation = CampaignCandidateService._ai_evaluation(campaign_candidate)
        recommendation = getattr(ai_evaluation, "ai_recommendation", None) if ai_evaluation else None
        strengths = getattr(ai_evaluation, "ai_strengths", None) if ai_evaluation else None
        weaknesses = getattr(ai_evaluation, "ai_weaknesses", None) if ai_evaluation else None
        if recommendation is None and not strengths and not weaknesses:
            return None
        return AiSummaryDetail(
            recommendation=recommendation.value if recommendation is not None else None,
            strengths=strengths,
            weaknesses=weaknesses,
        )

    def get_candidate_deterministic(self, campaign_candidate_id: UUID) -> CandidateDeterministicResponse:
        """
        Deterministic-tab-only view: deterministic_score +
        deterministic_score_breakdown, reusing
        _build_deterministic_score_breakdown exactly as-is (the same
        object the full scorecard's deterministic_score_breakdown field
        carries). Never includes summary/resume/semantic/AI-evaluation/
        final-status data.
        """
        campaign_candidate = self.campaign_candidate_repo.get_by_id(campaign_candidate_id)
        if not campaign_candidate:
            raise CampaignException("Campaign candidate not found.", 404)

        banner = self._build_rejection_banner(campaign_candidate)
        deterministic_score_breakdown = self._build_deterministic_score_breakdown(
            campaign_candidate, rejection_reason=banner.get("rejection_reason"),
        )

        return CandidateDeterministicResponse(
            campaign_candidate_id=campaign_candidate.id,
            deterministic_score=(
                float(campaign_candidate.deterministic_score)
                if campaign_candidate.deterministic_score is not None else None
            ),
            deterministic_score_breakdown=deterministic_score_breakdown,
        )

    def get_candidate_semantic(self, campaign_candidate_id: UUID) -> CandidateSemanticResponse:
        """
        Semantic-tab-only view: semantic_score + semantic_score_breakdown,
        mirroring get_candidate_deterministic exactly - a pure read/transform
        of campaign_candidates.semantic_score/semantic_score_breakdown
        (written by SemanticScoringService/calculate_semantic_score_task,
        M08-E02). Never recalculates anything, never touches resume/JD
        embeddings or pgvector - those live entirely in the Celery task.
        Never includes summary/resume/deterministic/AI-evaluation/
        final-status data.
        """
        campaign_candidate = self.campaign_candidate_repo.get_by_id(campaign_candidate_id)
        if not campaign_candidate:
            raise CampaignException("Campaign candidate not found.", 404)

        return CandidateSemanticResponse(
            campaign_candidate_id=campaign_candidate.id,
            semantic_score=(
                float(campaign_candidate.semantic_score)
                if campaign_candidate.semantic_score is not None else None
            ),
            semantic_score_breakdown=self._build_semantic_score_breakdown(campaign_candidate),
        )

    def get_candidate_ai_evaluation(self, campaign_candidate_id: UUID) -> CandidateAIEvaluationResponse:
        """
        AI-Evaluation-tab-only view: a pure read of the campaign_candidates.
        ai_* columns written by AIEvaluationService.calculate_and_store_
        evaluation (Phase 2.4), mirroring get_candidate_deterministic/
        get_candidate_semantic exactly. Never recalculates anything, never
        calls Gemini - that lives entirely in calculate_ai_evaluation_task.
        Never includes summary/resume/deterministic/semantic/final-status
        data.
        """
        campaign_candidate = self.campaign_candidate_repo.get_by_id(campaign_candidate_id)
        if not campaign_candidate:
            raise CampaignException("Campaign candidate not found.", 404)

        ai_evaluation = self._ai_evaluation(campaign_candidate)
        return CandidateAIEvaluationResponse(
            campaign_candidate_id=campaign_candidate.id,
            ai_evaluation_status=(
                ai_evaluation.ai_evaluation_status if ai_evaluation else AIEvaluationStatus.PENDING
            ),
            effective_ai_score=(
                float(ai_evaluation.effective_ai_score)
                if ai_evaluation and ai_evaluation.effective_ai_score is not None else None
            ),
            ai_confidence=(
                float(ai_evaluation.ai_confidence)
                if ai_evaluation and ai_evaluation.ai_confidence is not None else None
            ),
            ai_recommendation=ai_evaluation.ai_recommendation if ai_evaluation else None,
            ai_strengths=ai_evaluation.ai_strengths if ai_evaluation else None,
            ai_weaknesses=ai_evaluation.ai_weaknesses if ai_evaluation else None,
            ai_response_json=ai_evaluation.ai_response_json if ai_evaluation else None,
        )

    @staticmethod
    def _build_semantic_score_breakdown(
        campaign_candidate: CampaignCandidate,
    ) -> SemanticScoreBreakdownResponse | None:
        breakdown = campaign_candidate.semantic_breakdown
        if not breakdown:
            return None

        passed = breakdown.get("semantic_passed")
        return SemanticScoreBreakdownResponse(
            summary=SemanticScoreSummary(
                overall_score=breakdown.get("semantic_score"),
                status="PASSED" if passed else "FAILED",
                threshold=breakdown.get("semantic_threshold"),
                matching_skills_count=len(breakdown.get("matching_skills") or []),
                missing_skills_count=len(breakdown.get("missing_skills") or []),
                matched_keywords_count=len(breakdown.get("matched_keywords") or []),
                screened_at=breakdown.get("computed_at"),
                failure_reason=breakdown.get("semantic_explanation") if not passed else None,
            ),
            overall_similarity=breakdown.get("overall_similarity"),
            semantic_passed=passed,
            semantic_threshold=breakdown.get("semantic_threshold"),
            matching_skills=breakdown.get("matching_skills") or [],
            missing_skills=breakdown.get("missing_skills") or [],
            matched_keywords=breakdown.get("matched_keywords") or [],
            semantic_explanation=breakdown.get("semantic_explanation"),
        )

    def _build_processing_timeline(self, campaign_candidate_id: UUID) -> list[ProcessingTimelineEntry]:
        """
        Epic 4 (M05-E04) Phase D2 - every celery_task_log row for this
        candidate, oldest first. Read-only; nothing here writes to
        celery_task_log or recalculates anything the tasks themselves
        already recorded.
        """
        if self.celery_task_log_service is None:
            return []

        logs = self.celery_task_log_service.repository.get_by_campaign_candidate_id(campaign_candidate_id)

        return [
            ProcessingTimelineEntry(
                task_type=log.task_type,
                status=log.status.value,
                queued_at=log.queued_at,
                started_at=log.started_at,
                completed_at=log.completed_at,
                duration_display=self._format_duration(log.started_at, log.completed_at),
                error_message=log.error_message,
            )
            for log in logs
        ]

    @staticmethod
    def _format_duration(started_at: datetime | None, completed_at: datetime | None) -> str | None:
        if started_at is None or completed_at is None:
            return None
        seconds = (completed_at - started_at).total_seconds()
        if seconds < 60:
            return f"{seconds:.1f} seconds"
        minutes, remaining_seconds = divmod(int(seconds), 60)
        return f"{minutes}m {remaining_seconds}s"

    def _build_rejection_banner(self, campaign_candidate: CampaignCandidate) -> dict:
        is_overridden = campaign_candidate.decision_type == DecisionType.RESET
        has_rejection = False
        rejection_layer = None
        rejection_reason = None
        rejected_at = None

        # M07-E03 S04: a real override (S04 T02) moves pipeline_stage away
        # from REJECTED, so the lookup must still run for an overridden
        # candidate - otherwise "preserving original rejection_reason and
        # rejected_at" (this story's own S03 T01 requirement) would go
        # unmet the moment an override actually happens. has_rejection
        # itself stays scoped to "currently REJECTED" - only the
        # reason/timestamp/layer are preserved once overridden.
        #
        # Currently REJECTED: the candidate's own decision_* fields ARE the
        # latest rejection (no lookup needed anymore). Overridden: the
        # rejection being preserved lives in decision_details (captured by
        # StageTransitionService.apply_hr_override before it overwrote
        # decision_*  with the RESET decision) - decision_* itself now
        # describes the override, not the original rejection.
        if campaign_candidate.pipeline_stage == PipelineStage.REJECTED:
            if campaign_candidate.decision_source == _SCORECARD_BANNER_DECISION_SOURCE:
                has_rejection = True
                rejection_layer = campaign_candidate.decision_source
                rejection_reason = campaign_candidate.decision_reason
                rejected_at = campaign_candidate.decision_at
        elif is_overridden:
            details = campaign_candidate.decision_details or {}
            overridden_source = details.get("overridden_decision_source")
            if overridden_source == _SCORECARD_BANNER_DECISION_SOURCE.value:
                rejection_layer = DecisionSource(overridden_source)
                rejection_reason = details.get("overridden_decision_reason")
                overridden_at = details.get("overridden_decision_at")
                rejected_at = datetime.fromisoformat(overridden_at) if overridden_at else None

        return {
            "has_rejection": has_rejection,
            "rejection_layer": rejection_layer,
            "rejection_reason": rejection_reason,
            "rejected_at": rejected_at,
            "score_breakdown": campaign_candidate.deterministic_breakdown,
            "is_overridden": is_overridden,
            "status": "Overridden — Previously Rejected" if is_overridden else None,
        }

    # ------------------------------------------------------------------
    # Deterministic Score API response contract: UI-friendly restructuring
    # of the already-computed/stored score_breakdown. Pure read/transform -
    # no scoring logic, no recalculation, no repository/service/Celery
    # changes. Every value is either read directly from score_breakdown (or
    # CampaignCandidate/PlatformConfig) or is a simple display-formatting
    # transform (status strings, degree-level display names) of an
    # already-computed value.
    # ------------------------------------------------------------------

    def _build_deterministic_score_breakdown(
        self, campaign_candidate: CampaignCandidate, rejection_reason: str | None = None,
    ) -> DeterministicScoreBreakdownResponse | None:
        breakdown = campaign_candidate.deterministic_breakdown
        if not breakdown:
            return None

        mandatory_skills_raw = breakdown.get("mandatory_skills") or []
        preferred_skills_raw = breakdown.get("preferred_skills") or []
        experience = breakdown.get("experience_validation")
        education = breakdown.get("education_validation")

        mandatory_skills = [
            MandatorySkillBreakdownItem(
                jd_skill=entry.get("canonical_name"),
                candidate_skill=entry.get("matched_candidate_skill_canonical_name"),
                mandatory=entry.get("mandatory"),
                match_type=entry.get("match_type"),
                configured_weight=entry.get("configured_weight"),
                normalization_discount=entry.get("candidate_scoring_weight"),
                hierarchy_multiplier=entry.get("hierarchy_score_multiplier"),
                contribution=entry.get("skill_contribution"),
                confidence=entry.get("confidence"),
                passed=entry.get("match_type") != "MISSING",
                matched=entry.get("match_type") != "MISSING",
                match_reason=self._skill_match_reason(
                    entry.get("match_type"), entry.get("matched_candidate_skill_canonical_name"),
                ),
                contribution_percentage=self._contribution_percentage(
                    entry.get("skill_contribution"), entry.get("configured_weight"),
                ),
            )
            for entry in mandatory_skills_raw
        ]

        preferred_skills = [
            PreferredSkillBreakdownItem(
                jd_skill=entry.get("canonical_name"),
                candidate_skill=entry.get("matched_candidate_skill_canonical_name"),
                match_type=entry.get("match_type"),
                configured_weight=entry.get("configured_weight"),
                bonus=entry.get("skill_contribution"),
                confidence=entry.get("confidence"),
                matched=entry.get("match_type") != "MISSING",
                match_reason=self._skill_match_reason(
                    entry.get("match_type"), entry.get("matched_candidate_skill_canonical_name"),
                ),
                contribution_percentage=self._contribution_percentage(
                    entry.get("skill_contribution"), entry.get("configured_weight"),
                ),
            )
            for entry in preferred_skills_raw
        ]

        missing_mandatory_skills = [
            MissingMandatorySkillItem(
                skill=entry.get("canonical_name"),
                configured_weight=entry.get("configured_weight"),
                reason="No matching skill found in candidate's profile (including hierarchy fallback).",
            )
            for entry in mandatory_skills_raw
            if entry.get("match_type") == "MISSING"
        ]

        # Populated from both mandatory and preferred mappings, per this
        # contract - preferred skills never actually carry a hierarchy
        # match_type today (EXACT-only), so this is a no-op extension in
        # practice, not a new query or new matching behavior.
        hierarchy_matches = [
            HierarchyMatchItem(
                jd_skill=entry.get("canonical_name"),
                candidate_skill=entry.get("matched_candidate_skill_canonical_name"),
                relationship=entry.get("match_type"),
                multiplier=entry.get("hierarchy_score_multiplier"),
                match_type=entry.get("match_type"),
                hierarchy_multiplier=entry.get("hierarchy_score_multiplier"),
            )
            for entry in (mandatory_skills_raw + preferred_skills_raw)
            if entry.get("match_type") in _HIERARCHY_RELATIONSHIP_MATCH_TYPES
        ]

        mandatory_matched = sum(
            1 for entry in mandatory_skills_raw if entry.get("match_type") != "MISSING"
        )
        preferred_matched = sum(
            1 for entry in preferred_skills_raw if entry.get("match_type") != "MISSING"
        )

        config_values = (
            self.config_repo.get_configs_by_keys([
                _DETERMINISTIC_WEIGHT_SKILLS_KEY,
                _DETERMINISTIC_WEIGHT_EXPERIENCE_KEY,
                _DETERMINISTIC_WEIGHT_EDUCATION_KEY,
                _HIERARCHY_SEMANTIC_ONLY_THRESHOLD_KEY,
                _HIERARCHY_GRANDCHILD_MULTIPLIER_KEY,
                _HIERARCHY_CHILD_MULTIPLIER_KEY,
                _HIERARCHY_SIBLING_MULTIPLIER_KEY,
                _SEMANTIC_MULTIPLIER_KEY,
            ])
            if self.config_repo is not None else {}
        )

        experience_min_years = experience.get("min_years") if experience else None
        experience_effective_min_years = experience.get("effective_min_years") if experience else None
        experience_tolerance = (
            round(experience_min_years - experience_effective_min_years, 2)
            if experience_min_years is not None and experience_effective_min_years is not None
            else None
        )

        from app.services.campaign.candidate_scoring_service import _degree_level_display

        skills_score = breakdown.get("skill_deterministic_score")
        if skills_score is None:
            skills_score = breakdown.get("deterministic_score")

        # Reuses candidate_rejections.rejection_reason exactly as already
        # resolved by _build_rejection_banner (passed in by the caller) -
        # never a second, independent lookup. Split on " | ", the same
        # delimiter CandidateScoringService.build_rejection_reason already
        # concatenates multiple failure clauses with.
        failure_reasons = rejection_reason.split(" | ") if rejection_reason else []

        return DeterministicScoreBreakdownResponse(
            summary=DeterministicScoreSummary(
                overall_score=breakdown.get("deterministic_score"),
                status="PASSED" if breakdown.get("deterministic_passed") else "FAILED",
                threshold=breakdown.get("deterministic_threshold"),
                mandatory_coverage_pct=breakdown.get("mandatory_coverage_pct"),
                mandatory_skills_matched=mandatory_matched,
                mandatory_skills_total=len(mandatory_skills_raw),
                preferred_skills_matched=preferred_matched,
                preferred_skills_total=len(preferred_skills_raw),
                additional_skills_count=None,
                experience_status=self._validation_status(experience),
                education_status=self._validation_status(education),
                screened_at=getattr(campaign_candidate, "screened_at", None),
                failure_reason=rejection_reason,
                failure_reasons=failure_reasons,
                screening_completed_at=getattr(campaign_candidate, "screened_at", None),
            ),
            missing_mandatory_skills=missing_mandatory_skills,
            mandatory_skills=mandatory_skills,
            preferred_skills=preferred_skills,
            additional_candidate_skills=[],
            hierarchy_matches=hierarchy_matches,
            experience_validation=ExperienceValidationDetail(
                required_years=experience_min_years,
                candidate_years=experience.get("candidate_years") if experience else None,
                tolerance=experience_tolerance,
                passed=experience.get("passed") if experience else None,
                status=self._detailed_validation_status(experience),
            ),
            education_validation=EducationValidationDetail(
                required_degree=(
                    _degree_level_display(education.get("required_level")) if education else None
                ),
                candidate_degree=(
                    _degree_level_display(education.get("candidate_level")) if education else None
                ),
                equivalent_experience_applied=(
                    education.get("equivalent_experience_applied") if education else None
                ),
                passed=education.get("passed") if education else None,
            ),
            score_calculation=ScoreCalculationDetail(
                skills_score=skills_score,
                experience_score=experience.get("score") if experience else None,
                education_score=education.get("score") if education else None,
                final_score=breakdown.get("deterministic_score"),
            ),
            configuration=ScoreConfigurationDetail(
                skills_weight=self._config_float(config_values, _DETERMINISTIC_WEIGHT_SKILLS_KEY),
                experience_weight=self._config_float(config_values, _DETERMINISTIC_WEIGHT_EXPERIENCE_KEY),
                education_weight=self._config_float(config_values, _DETERMINISTIC_WEIGHT_EDUCATION_KEY),
                deterministic_threshold=breakdown.get("deterministic_threshold"),
                semantic_threshold=self._config_float(config_values, _HIERARCHY_SEMANTIC_ONLY_THRESHOLD_KEY),
                hierarchy_grandchild_multiplier=self._config_float(
                    config_values, _HIERARCHY_GRANDCHILD_MULTIPLIER_KEY,
                ),
                hierarchy_child_multiplier=self._config_float(config_values, _HIERARCHY_CHILD_MULTIPLIER_KEY),
                hierarchy_sibling_multiplier=self._config_float(config_values, _HIERARCHY_SIBLING_MULTIPLIER_KEY),
                semantic_multiplier=self._config_float(config_values, _SEMANTIC_MULTIPLIER_KEY),
            ),
        )

    @staticmethod
    def _validation_status(result: dict | None) -> str | None:
        if result is None:
            return None
        if result.get("skipped"):
            return "NOT_REQUIRED"
        if result.get("data_missing"):
            return "DATA_MISSING"
        return "PASSED" if result.get("passed") else "FAILED"

    @staticmethod
    def _detailed_validation_status(result: dict | None) -> str | None:
        """
        Same underlying applicable/skipped/data_missing/passed flags as
        _validation_status, but PASSED/FAILED/DATA_MISSING/SKIPPED vocabulary
        for ExperienceValidationDetail.status specifically - a separate
        method (not a shared one) because _validation_status's
        "NOT_REQUIRED" value is already an existing, shipped field
        (summary.experience_status) that must not change.
        """
        if result is None:
            return None
        if result.get("skipped"):
            return "SKIPPED"
        if result.get("data_missing"):
            return "DATA_MISSING"
        return "PASSED" if result.get("passed") else "FAILED"

    @staticmethod
    def _skill_match_reason(match_type: str | None, candidate_skill: str | None) -> str | None:
        if match_type is None:
            return None
        if match_type == "MISSING":
            return "No matching skill found in candidate's profile (including hierarchy fallback)."
        if match_type == "EXACT":
            return f"Exact match with candidate skill '{candidate_skill}'." if candidate_skill else "Exact match."
        label = _SKILL_MATCH_TIER_LABELS.get(match_type, "a related skill")
        return f"Matched via {label} '{candidate_skill}'." if candidate_skill else f"Matched via {label}."

    @staticmethod
    def _contribution_percentage(contribution: float | None, configured_weight: float | None) -> float | None:
        if contribution is None or not configured_weight:
            return None
        return round((contribution / configured_weight) * 100, 2)

    @staticmethod
    def _config_float(config_values: dict, key: str) -> float | None:
        raw = config_values.get(key)
        return float(raw) if raw is not None else None

    # ------------------------------------------------------------------
    # M07-E03 S03 T02: Rejection History (read-only)
    # ------------------------------------------------------------------

    def get_rejection_history(
        self,
        campaign_candidate_id: UUID,
    ) -> list[CandidateRejectionHistoryEntryResponse]:
        """
        Every to_stage=REJECTED campaign_candidate_stage_history row for
        this campaign_candidate, newest first - candidate_rejections is
        gone, but every rejection transition's scores_snapshot was
        enriched with decision_source/decision_reason at write time
        (StageTransitionService), so this reads the same information from
        there instead. Read-only: no edit/delete endpoint exists or is
        added here.
        """
        campaign_candidate = self.campaign_candidate_repo.get_by_id(campaign_candidate_id)
        if not campaign_candidate:
            raise CampaignException("Campaign candidate not found.", 404)

        history_rows = self.campaign_candidate_repo.get_stage_history_by_campaign_candidate_id(
            campaign_candidate_id,
        )
        rejection_rows = [row for row in history_rows if row.to_stage == PipelineStage.REJECTED]
        rejection_rows.reverse()  # oldest-first -> newest-first, matching the old ordering
        total = len(rejection_rows)
        is_overridden = campaign_candidate.decision_type == DecisionType.RESET

        entries = []
        for index, row in enumerate(rejection_rows):
            snapshot = row.scores_snapshot or {}
            decision_source = snapshot.get("decision_source")
            entries.append(CandidateRejectionHistoryEntryResponse(
                id=row.id,
                rejection_layer=DecisionSource(decision_source) if decision_source else DecisionSource.SYSTEM,
                rejection_reason=snapshot.get("decision_reason") or "",
                rejected_at=row.changed_at,
                hr_override=is_overridden,
                evaluation_round=total - index,  # oldest=1, newest=total
                current_status=(index == 0),
            ))
        return entries

    # ------------------------------------------------------------------
    # M10-E03 Phase 3: Export Campaign Ranked Candidate List (HR_ADMIN only - enforced at the route)
    # ------------------------------------------------------------------

    def export_ranked_campaign_candidates(
        self,
        campaign_id: UUID,
        actor_id: str,
        actor_role: str | None,
        sort_by: str | None = None,
        sort_order: str = "desc",
        pipeline_stage: PipelineStage | None = None,
        composite_score_min: float | None = None,
        composite_score_max: float | None = None,
        ai_recommendation: AIRecommendation | None = None,
        ai_evaluation_status: AIEvaluationStatus | None = None,
        include_pending: bool = True,
        include_rejected: bool = True,
        include_fraud: bool = True,
        hr_override: bool | None = None,
    ) -> StreamingResponse:
        """
        M10-E03 Phase 3: exports the campaign's COMPLETE filtered/sorted
        ranked candidate list to XLSX - pagination is deliberately ignored
        (every matching candidate, never one UI page). Reuses
        CampaignCandidateRepository.get_ranked_by_campaign() exactly, the
        same filters/ordering get_ranked_campaign_candidates() (Phase 1)
        already validates and delegates to - no second/duplicate ranking
        query, no Python-side sorting, no recalculated scores. Called
        twice: once with page_size=1 to learn the filtered `total` cheaply,
        then (only if total > 0) once more with page_size=total to fetch
        every matching row in one page - both calls hit the exact same,
        unmodified repository method.

        Reuses ExcelExport (no new XLSX engine) and AuditService, the same
        StreamingResponse convention as export_rejected_candidates/
        export_override_report. Never includes candidate name/email/phone/
        resume - only the opaque candidate_uuid plus ranking/score fields
        already computed and persisted by the scoring pipeline.
        """
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException("Campaign not found.", 404)

        if (
            composite_score_min is not None
            and composite_score_max is not None
            and composite_score_min > composite_score_max
        ):
            raise CampaignException("composite_score_min must not be greater than composite_score_max.", 422)

        filters = dict(
            sort_by=sort_by,
            sort_order=sort_order,
            pipeline_stage=pipeline_stage,
            composite_score_min=composite_score_min,
            composite_score_max=composite_score_max,
            ai_recommendation=ai_recommendation,
            ai_evaluation_status=ai_evaluation_status,
            include_pending=include_pending,
            include_rejected=include_rejected,
            include_fraud=include_fraud,
            hr_override=hr_override,
        )

        _, total = self.campaign_candidate_repo.get_ranked_by_campaign(campaign_id, page=1, page_size=1, **filters)
        rows = []
        if total > 0:
            rows, _ = self.campaign_candidate_repo.get_ranked_by_campaign(
                campaign_id, page=1, page_size=total, **filters,
            )

        export_rows = [
            self._to_ranking_export_row(campaign_candidate, rank)
            for rank, (campaign_candidate, _candidate, _resume) in enumerate(rows, start=1)
        ]

        excel_file = ExcelExport.export_candidate_ranking(export_rows)
        filename = f"candidate_ranking_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

        self.audit_service.log(
            actor_id=actor_id,
            actor_role=actor_role,
            action_type=ActionType.CANDIDATE_RANKING_EXPORTED,
            entity_type=EntityType.CAMPAIGN,
            entity_id=campaign_id,
            campaign_id=campaign_id,
            details={
                "export_format": "XLSX",
                "applied_filters": {
                    key: (value.value if hasattr(value, "value") else value)
                    for key, value in filters.items()
                },
                "rows_exported": len(export_rows),
            },
        )

        return StreamingResponse(
            excel_file,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @staticmethod
    def _to_ranking_export_row(campaign_candidate: CampaignCandidate, rank: int) -> dict:
        """
        M10-E03 Phase 3: one row per candidate in the campaign's complete
        ranking export. Only ranking/score fields already computed and
        persisted by the scoring pipeline - never candidate name/email/
        phone/resume or any other PII, matching every other export in this
        codebase. ranking_status reuses _derive_ranking_status verbatim -
        no second status concept.
        """
        ai_evaluation = CampaignCandidateService._ai_evaluation(campaign_candidate)
        return {
            "rank": rank,
            "candidate_uuid": str(campaign_candidate.candidate_id),
            "composite_score": (
                float(campaign_candidate.composite_score)
                if campaign_candidate.composite_score is not None else None
            ),
            "deterministic_score": (
                float(campaign_candidate.deterministic_score)
                if campaign_candidate.deterministic_score is not None else None
            ),
            "semantic_score": (
                float(campaign_candidate.semantic_score)
                if campaign_candidate.semantic_score is not None else None
            ),
            "ai_evaluation_score": (
                float(ai_evaluation.effective_ai_score)
                if ai_evaluation and ai_evaluation.effective_ai_score is not None else None
            ),
            "pipeline_stage": campaign_candidate.pipeline_stage.value,
            "ai_recommendation": (
                ai_evaluation.ai_recommendation.value
                if ai_evaluation and ai_evaluation.ai_recommendation is not None else ""
            ),
            "ranking_status": CampaignCandidateService._derive_ranking_status(campaign_candidate),
            "composite_score_computed_at": getattr(campaign_candidate, "composite_score_computed_at", None),
        }

    # ------------------------------------------------------------------
    # M07-E03 S03 T03: Export Rejected Candidates (HR_ADMIN only - enforced at the route)
    # ------------------------------------------------------------------

    def export_rejected_candidates(
        self,
        campaign_id: UUID,
        actor_id: str,
        actor_role: str | None,
    ) -> StreamingResponse:
        """
        Reuses ExcelExport (no new XLSX engine) and AuditService (no
        manual audit_log insert) - same StreamingResponse convention as
        SkillOntologyService.export_skills. Never includes candidate name/
        email/phone/resume - only the opaque candidate_id plus rejection/
        score fields, all already computed by the deterministic scoring
        pipeline, never recalculated here.
        """
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException("Campaign not found.", 404)

        rejected_candidates = self.campaign_candidate_repo.get_rejected_by_campaign(campaign_id)

        rows = [
            self._to_export_row(campaign_candidate)
            for campaign_candidate in rejected_candidates
        ]

        excel_file = ExcelExport.export_rejected_candidates(rows)
        filename = f"rejected_candidates_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

        self.audit_service.log(
            actor_id=actor_id,
            actor_role=actor_role,
            action_type=ActionType.REJECTED_CANDIDATES_EXPORTED,
            entity_type=EntityType.CAMPAIGN,
            entity_id=campaign_id,
            campaign_id=campaign_id,
            details={"exported_count": len(rows)},
        )

        return StreamingResponse(
            excel_file,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    def _to_export_row(self, campaign_candidate: CampaignCandidate) -> dict:
        """
        One row per rejected candidate, scoped to this story's exact
        DETERMINISTIC-layer condition (matching T01's scorecard banner
        rule). missing_mandatory_skills/experience_gap/education_gap are
        the deterministic_breakdown sub-fields called out explicitly as
        required export columns - derived from the same breakdown already
        computed and stored, never recalculated.

        Every row here is currently pipeline_stage == REJECTED (from
        get_rejected_by_campaign), which can never simultaneously be
        decision_type == RESET (an override always moves pipeline_stage
        away from REJECTED) - so decision_* IS the current rejection, read
        directly with no separate lookup, and hr_override/override_reason
        always evaluate to their "not overridden" values here, matching
        the pre-refactor behavior exactly.
        """
        breakdown = campaign_candidate.deterministic_breakdown or {}

        return {
            "candidate_uuid": str(campaign_candidate.candidate_id),
            "rejection_layer": campaign_candidate.decision_source.value if campaign_candidate.decision_source else "",
            "rejection_reason": campaign_candidate.decision_reason or "",
            "rejected_at": campaign_candidate.decision_at,
            "deterministic_score": (
                float(campaign_candidate.deterministic_score)
                if campaign_candidate.deterministic_score is not None else None
            ),
            "missing_mandatory_skills": self._missing_mandatory_skills_display(breakdown),
            "experience_gap": self._experience_gap_display(breakdown),
            "education_gap": self._education_gap_display(breakdown),
            "hr_override": campaign_candidate.decision_type == DecisionType.RESET,
            "override_reason": campaign_candidate.decision_reason if campaign_candidate.decision_type == DecisionType.RESET else "",
        }

    @staticmethod
    def _missing_mandatory_skills_display(breakdown: dict) -> str:
        missing = [
            skill.get("canonical_name") or skill.get("canonical_skill_id")
            for skill in breakdown.get("mandatory_skills", [])
            if skill.get("match_type") == "MISSING"
        ]
        return ", ".join(missing)

    @staticmethod
    def _experience_gap_display(breakdown: dict) -> str:
        experience_result = breakdown.get("experience_validation")
        if not experience_result or experience_result.get("passed"):
            return ""
        candidate_years = experience_result.get("candidate_years")
        min_years = experience_result.get("min_years")
        if candidate_years is None or min_years is None:
            return ""
        gap = round(min_years - candidate_years, 1)
        return f"{candidate_years} years provided, {min_years} years required (gap: {gap} years)"

    @staticmethod
    def _education_gap_display(breakdown: dict) -> str:
        education_result = breakdown.get("education_validation")
        if not education_result or education_result.get("passed"):
            return ""
        from app.services.campaign.candidate_scoring_service import _degree_level_display
        required = _degree_level_display(education_result.get("required_level"))
        found = _degree_level_display(education_result.get("candidate_level"))
        return f"{required} required, {found} found"

    # ------------------------------------------------------------------
    # M07-E03 S04 T01/T02: HR_ADMIN Override of a Deterministic Rejection
    # ------------------------------------------------------------------

    # Mirrors JDService.EXPORT_AUDIT_ENTITY_ID / BulkUploadService's
    # EXPORT_AUDIT_ENTITY_ID exactly - a sentinel entity_id for an export
    # that isn't scoped to a single entity (audit_log.entity_id is
    # NOT NULL).
    EXPORT_AUDIT_ENTITY_ID = UUID("00000000-0000-0000-0000-000000000000")

    def apply_hr_override(
        self,
        campaign_candidate_id: UUID,
        override_reason: str,
        actor_id: str,
        actor_role: str | None = None,
    ) -> CandidateScorecardResponse:
        """
        HR_ADMIN override of a deterministic OR semantic rejection (M08-E02
        S03 T03 extends this beyond deterministic-only) - re-enters the
        candidate into SCREENING. Applies only to this single
        campaign_candidate_id; never touches any other candidate or
        campaign. The rejection being overridden is never deleted - it's
        captured into decision_details before being replaced by the RESET
        decision (T02: Rejection History already shows both, via
        campaign_candidate_stage_history).
        """
        try:
            campaign_candidate = self.campaign_candidate_repo.get_by_id(campaign_candidate_id)
            if not campaign_candidate:
                raise CampaignException("Campaign candidate not found.", 404)

            if campaign_candidate.pipeline_stage != PipelineStage.REJECTED:
                raise CampaignException(
                    "HR override can only be applied to a candidate currently in the REJECTED stage.",
                    409,
                )

            if campaign_candidate.decision_type != DecisionType.REJECTED or campaign_candidate.decision_source not in (
                DecisionSource.DETERMINISTIC, DecisionSource.SEMANTIC,
            ):
                raise CampaignException(
                    "HR override is only available for candidates rejected at the "
                    "deterministic or semantic layer.",
                    409,
                )

            campaign_candidate.deterministic_passed = True
            ai_evaluation = self.ai_evaluation_repo.get_or_create(campaign_candidate.id)
            ai_evaluation.ai_evaluation_status = AIEvaluationStatus.PENDING
            self.ai_evaluation_repo.update(ai_evaluation)
            self.campaign_candidate_repo.update(campaign_candidate)

            if self.stage_transition_service is None:
                raise CampaignException("Stage transition service is not configured.", 500)

            # Validated against allowed_transitions (REJECTED -> SCREENING)
            # before the stage actually changes - reuses StageTransitionService,
            # extended with apply_hr_override rather than duplicated. Captures
            # the rejection being overridden into campaign_candidate.
            # decision_details before overwriting decision_* with the RESET
            # decision - read back below for the audit log, rather than
            # snapshotting the prior state ourselves.
            transitioned = self.stage_transition_service.apply_hr_override(
                campaign_candidate,
                changed_by=actor_id,
                change_reason=_HR_OVERRIDE_CHANGE_REASON,
                decision_reason=override_reason,
            )
            if not transitioned:
                raise CampaignException(
                    "Stage transition REJECTED -> SCREENING is not allowed - override not applied.",
                    409,
                )

            overridden = campaign_candidate.decision_details or {}

            # Story 543: no separate SEMANTIC_OVERRIDE_APPLIED action type
            # exists (and adding one is an unnecessary enum/migration for
            # what audit_log.details already records) - reuses
            # DETERMINISTIC_OVERRIDE_APPLIED for both layers, with
            # overridden_layer distinguishing which one in the detail.
            self.audit_service.log(
                actor_id=actor_id,
                actor_role=actor_role,
                action_type=ActionType.DETERMINISTIC_OVERRIDE_APPLIED,
                entity_type=EntityType.CAMPAIGN_CANDIDATE,
                entity_id=campaign_candidate.id,
                campaign_id=campaign_candidate.campaign_id,
                details={
                    "override_reason": override_reason,
                    "original_rejection_reason": overridden.get("overridden_decision_reason"),
                    "overridden_layer": overridden.get("overridden_decision_source"),
                },
            )

            self.campaign_candidate_repo.commit()

            # Best-effort, after commit - never undoes the already-committed
            # override (same reasoning as _queue_rejection_email, M07-E03 S02 T02).
            self._queue_post_override_evaluation(campaign_candidate)

            return self.get_campaign_candidate_scorecard(campaign_candidate_id)

        except Exception:
            self.campaign_candidate_repo.rollback()
            raise

    def _queue_post_override_evaluation(self, campaign_candidate: CampaignCandidate) -> None:
        """
        Story 543: routes on whether a semantic_score already exists -
        never queues AI_EVALUATE ahead of semantic scoring, mirroring the
        same PASS-gates-AI_EVALUATE ordering
        calculate_semantic_score_task's own auto-trigger enforces
        (app.tasks.semantic_scoring_tasks._queue_ai_evaluate_if_not_duplicate).
        - semantic_score already set (candidate was rejected AT the
          semantic layer, or was re-scored since) -> AI_EVALUATE is queued
          immediately, since semantic has already run.
        - semantic_score missing (a deterministic-layer override, or any
          candidate whose semantic score was never computed) -> SEMANTIC_SCORE
          is enqueued instead; AI_EVALUATE is queued automatically once that
          later passes (same shared pass-path as the normal pipeline), never
          queued upfront here.
        """
        if self.celery_task_log_service is None:
            return
        try:
            if campaign_candidate.semantic_score is not None:
                self._queue_task_log_if_not_duplicate(campaign_candidate, AI_EVALUATE_TASK_TYPE)
                return

            # M08-E02: reuses the exact same enqueue/idempotency helper
            # calculate_deterministic_score_task's own auto-trigger uses
            # (app.tasks.semantic_scoring_tasks._enqueue_semantic_scoring) -
            # never a second/independent implementation of "how do I queue
            # semantic scoring for this candidate."
            if self.resume_repo is not None:
                campaign = (
                    self.campaign_repo.get_by_id(campaign_candidate.campaign_id)
                    if self.campaign_repo is not None else None
                )
                jd_id = campaign.jd_id if campaign is not None else None
                _enqueue_semantic_scoring(
                    campaign_candidate, self.celery_task_log_service, self.resume_repo, jd_id=jd_id,
                )

            # M10-E01: an HR override is NOT a composite-score trigger. It
            # only restarts the remaining scoring pipeline (re-queued above:
            # AI_EVALUATE + semantic scoring) - composite_score is
            # (re)computed only once that pipeline's AI evaluation step
            # eventually completes, or on a campaign weight change. Never
            # enqueue composite scoring directly from here.
        except Exception:
            logger.exception(
                "Failed to queue post-override evaluation tasks for campaign_candidate_id=%s",
                campaign_candidate.id,
            )

    def _queue_task_log_if_not_duplicate(self, campaign_candidate: CampaignCandidate, task_type: str):
        task_log_repo = self.celery_task_log_service.repository
        already_queued = any(
            log.status in (TaskStatus.QUEUED, TaskStatus.RUNNING)
            for log in task_log_repo.get_by_campaign_candidate_and_task_type(campaign_candidate.id, task_type)
        )
        if already_queued:
            return None
        return self.celery_task_log_service.create_log(
            task_id=str(uuid4()),
            task_type=task_type,
            campaign_candidate_id=campaign_candidate.id,
        )

    # ------------------------------------------------------------------
    # M07-E03 S04 T03: Override Report
    # ------------------------------------------------------------------

    def get_override_report(
        self,
        campaign_id: UUID | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> OverrideReportResponse:
        overridden = self.campaign_candidate_repo.get_overridden(
            campaign_id=campaign_id, date_from=date_from, date_to=date_to,
        )

        campaign_cache: dict[UUID, HiringCampaign | None] = {}
        hr_user_ids = [cc.decision_by_user_id for cc in overridden if cc.decision_by_user_id]
        # Reuses CampaignRepository.get_hiring_manager_names - despite its
        # name, it is a generic user_id -> full_name batch lookup (any list
        # of user ids) - reused here instead of adding a dedicated
        # UserRepository/method for this one lookup.
        hr_names = self.campaign_repo.get_hiring_manager_names(hr_user_ids)

        rows = []
        for cc in overridden:
            campaign = self._get_campaign_cached(cc.campaign_id, campaign_cache)
            rows.append(self._to_override_report_row(cc, campaign, hr_names))

        weekly_trend = self._compute_weekly_trend(campaign_id)
        campaign_alerts = self._compute_campaign_alerts(campaign_id, rows, campaign_cache)

        return OverrideReportResponse(
            rows=rows,
            total_count=len(rows),
            weekly_trend=weekly_trend,
            campaign_alerts=campaign_alerts,
        )

    def _get_campaign_cached(
        self, campaign_id: UUID, campaign_cache: dict[UUID, HiringCampaign | None],
    ) -> HiringCampaign | None:
        if campaign_id not in campaign_cache:
            campaign_cache[campaign_id] = self.campaign_repo.get_by_id(campaign_id)
        return campaign_cache[campaign_id]

    def _to_override_report_row(
        self,
        campaign_candidate: CampaignCandidate,
        campaign: HiringCampaign | None,
        hr_names: dict[str, str],
    ) -> OverrideReportRow:
        # The ORIGINAL (oldest) rejection reason, not the one captured in
        # decision_details (which only holds the immediately-preceding
        # decision) - candidate_rejections is gone, so this reads the
        # candidate's own stage history ascending and takes the first
        # to_stage=REJECTED transition's decision_reason, matching the old
        # "oldest = original (newest-first list)" convention exactly.
        original_reason = None
        history_rows = self.campaign_candidate_repo.get_stage_history_by_campaign_candidate_id(
            campaign_candidate.id,
        )
        first_rejection = next((row for row in history_rows if row.to_stage == PipelineStage.REJECTED), None)
        if first_rejection is not None:
            original_reason = (first_rejection.scores_snapshot or {}).get("decision_reason")

        return OverrideReportRow(
            campaign_id=campaign_candidate.campaign_id,
            campaign_name=campaign.name if campaign is not None else "",
            candidate_uuid=campaign_candidate.candidate_id,
            original_rejection_reason=original_reason,
            override_reason=campaign_candidate.decision_reason or "",
            hr_full_name=hr_names.get(campaign_candidate.decision_by_user_id),
            override_timestamp=campaign_candidate.decision_at,
            current_pipeline_stage=campaign_candidate.pipeline_stage,
        )

    def _compute_weekly_trend(self, campaign_id: UUID | None) -> list[OverrideWeeklyTrendPoint]:
        """
        Fixed last-8-weeks window (independent of the report's date_from/
        date_to row filters, per this story's "Trend: last 8 weeks" spec),
        Monday-anchored week buckets.
        """
        now = datetime.now(timezone.utc)
        current_week_start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        window_start = current_week_start - timedelta(weeks=_WEEKLY_TREND_WEEKS - 1)

        overridden = self.campaign_candidate_repo.get_overridden(
            campaign_id=campaign_id, date_from=window_start, date_to=None,
        )

        buckets = {
            (current_week_start - timedelta(weeks=offset)).date(): 0
            for offset in range(_WEEKLY_TREND_WEEKS)
        }
        for cc in overridden:
            if cc.decision_at is None:
                continue
            week_start = (cc.decision_at - timedelta(days=cc.decision_at.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0,
            ).date()
            if week_start in buckets:
                buckets[week_start] += 1

        return [
            OverrideWeeklyTrendPoint(week_start=week_start, override_count=count)
            for week_start, count in sorted(buckets.items())
        ]

    def _compute_campaign_alerts(
        self,
        campaign_id: UUID | None,
        rows: list[OverrideReportRow],
        campaign_cache: dict[UUID, HiringCampaign | None],
    ) -> list[CampaignOverrideAlert]:
        threshold = _DEFAULT_OVERRIDE_RATE_ALERT_THRESHOLD
        if self.config_repo is not None:
            configs = self.config_repo.get_configs_by_keys([_OVERRIDE_RATE_ALERT_THRESHOLD_KEY])
            raw_threshold = configs.get(_OVERRIDE_RATE_ALERT_THRESHOLD_KEY)
            if raw_threshold is not None:
                threshold = float(raw_threshold)

        campaign_ids = {campaign_id} if campaign_id is not None else {row.campaign_id for row in rows}

        override_counts: dict[UUID, int] = {}
        for row in rows:
            override_counts[row.campaign_id] = override_counts.get(row.campaign_id, 0) + 1

        alerts = []
        for cid in campaign_ids:
            campaign = self._get_campaign_cached(cid, campaign_cache)
            if campaign is None:
                continue

            override_count = override_counts.get(cid, 0)
            # Denominator: all-time rejected candidates in this campaign
            # (reuses S03's get_rejected_by_campaign) - override_rate
            # answers "of the candidates this campaign's deterministic
            # filter rejected, what fraction did HR decide to override",
            # which is what "review campaign JD skills or thresholds"
            # is actually about.
            rejected_count = len(self.campaign_candidate_repo.get_rejected_by_campaign(cid))
            override_rate = (override_count / rejected_count * 100) if rejected_count else 0.0
            alert = override_rate > threshold

            alerts.append(CampaignOverrideAlert(
                campaign_id=cid,
                campaign_name=campaign.name,
                override_count=override_count,
                rejected_count=rejected_count,
                override_rate=round(override_rate, 2),
                override_alert=alert,
                recommendation=_OVERRIDE_RECOMMENDATION if alert else None,
            ))

        return alerts

    def export_override_report(
        self,
        campaign_id: UUID | None,
        date_from: datetime | None,
        date_to: datetime | None,
        actor_id: str,
        actor_role: str | None,
    ) -> StreamingResponse:
        """
        Reuses ExcelExport (no new XLSX engine) and AuditService, same
        StreamingResponse convention as export_rejected_candidates. Never
        includes candidate name/email/phone/resume - only the opaque
        candidate_uuid. HR full name is not candidate PII and is included
        as explicitly required by this report.
        """
        report = self.get_override_report(campaign_id=campaign_id, date_from=date_from, date_to=date_to)

        rows = [self._to_override_export_row(row) for row in report.rows]

        excel_file = ExcelExport.export_override_report(rows)
        filename = f"override_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

        self.audit_service.log(
            actor_id=actor_id,
            actor_role=actor_role,
            action_type=ActionType.OVERRIDE_REPORT_EXPORTED,
            entity_type=EntityType.CAMPAIGN_CANDIDATE,
            entity_id=campaign_id if campaign_id is not None else self.EXPORT_AUDIT_ENTITY_ID,
            campaign_id=campaign_id,
            details={"exported_count": len(rows)},
        )

        return StreamingResponse(
            excel_file,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @staticmethod
    def _to_override_export_row(row: OverrideReportRow) -> dict:
        return {
            "campaign_name": row.campaign_name,
            "candidate_uuid": str(row.candidate_uuid),
            "original_rejection_reason": row.original_rejection_reason or "",
            "override_reason": row.override_reason,
            "hr_full_name": row.hr_full_name or "",
            "override_timestamp": row.override_timestamp,
            "current_pipeline_stage": row.current_pipeline_stage.value,
        }

    # ------------------------------------------------------------------
    # M07-E03 S05 T01/T02: Campaign Rejection Analytics + JD Calibration
    # ------------------------------------------------------------------

    def get_campaign_rejection_analytics(self, campaign_id: UUID) -> CampaignRejectionAnalyticsResponse:
        """
        Rejection-reason distribution, top missing mandatory skills, and
        (once MIN_CANDIDATES_FOR_ANALYTICS is reached) JD-calibration
        recommendations for one campaign. Computed entirely from
        campaign_candidate_stage_history's decision_details snapshots -
        never recalculated scoring, never touches deterministic_breakdown
        live.
        """
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignException("Campaign not found.", 404)

        rejection_rows = self.campaign_repo.get_deterministic_rejection_details(campaign_id=campaign_id)
        rejections = [detail for detail, _campaign_id in rejection_rows]
        total_rejections = len(rejections)

        breakdown = self._build_breakdown(rejections, total_rejections)
        top_missing_skills = self._build_top_missing_skills(rejections, total_rejections, limit=5)

        total_candidates = self.campaign_candidate_repo.get_candidate_count(campaign_id)
        min_candidates = self._get_min_candidates_for_analytics()

        recommendations = []
        if total_candidates >= min_candidates:
            recommendations = self._build_calibration_recommendations(
                campaign, breakdown, top_missing_skills,
            )

        return CampaignRejectionAnalyticsResponse(
            campaign_id=campaign_id,
            total_candidates=total_candidates,
            total_deterministic_rejections=total_rejections,
            min_candidates_for_analytics=min_candidates,
            breakdown=breakdown,
            top_missing_skills=top_missing_skills,
            recommendations=recommendations,
        )

    def _classify_rejection(self, rejection: dict) -> str | None:
        """
        Buckets one rejection into exactly one of the 7 mandatory-skill/
        experience/education failure combinations, reusing the exact same
        pass/fail logic already used to build the human-readable rejection
        reason (_missing_mandatory_skills_display/_experience_gap_display/
        _education_gap_display - each returns "" when that dimension did
        NOT fail). Returns None for the rare edge case where none of the
        three individually failed (e.g. the combined weighted score alone
        fell below threshold) - such rejections still count toward
        total_deterministic_rejections but are not part of the 7-bucket
        breakdown, since this story defines exactly those 7 buckets and no
        "other" catch-all.

        `rejection` is a decision_details dict (the deterministic scoring
        breakdown), read straight from campaign_candidate_stage_history's
        scores_snapshot now rather than a CandidateRejection.rejection_detail.
        """
        breakdown = rejection or {}
        skills_failed = bool(self._missing_mandatory_skills_display(breakdown))
        experience_failed = bool(self._experience_gap_display(breakdown))
        education_failed = bool(self._education_gap_display(breakdown))

        if skills_failed and experience_failed and education_failed:
            return _SKILLS_EXPERIENCE_EDUCATION
        if skills_failed and experience_failed:
            return _SKILLS_EXPERIENCE
        if skills_failed and education_failed:
            return _SKILLS_EDUCATION
        if experience_failed and education_failed:
            return _EXPERIENCE_EDUCATION
        if skills_failed:
            return _SKILLS_ONLY
        if experience_failed:
            return _EXPERIENCE_ONLY
        if education_failed:
            return _EDUCATION_ONLY
        return None

    def _build_breakdown(
        self, rejections: list[dict], total_rejections: int,
    ) -> list[RejectionBreakdownEntry]:
        category_counts: dict[str, int] = {}
        for rejection in rejections:
            category = self._classify_rejection(rejection)
            if category is None:
                continue
            category_counts[category] = category_counts.get(category, 0) + 1

        return [
            RejectionBreakdownEntry(
                category=category,
                count=category_counts.get(category, 0),
                percentage=(
                    round(category_counts.get(category, 0) / total_rejections * 100, 2)
                    if total_rejections else 0.0
                ),
            )
            for category in _BREAKDOWN_CATEGORY_ORDER
        ]

    @staticmethod
    def _aggregate_missing_skills(rejections: list[dict]) -> list[dict]:
        """
        One entry per distinct canonical skill that appeared as a MISSING
        mandatory skill across `rejections`, keyed by canonical_skill_id
        (falling back to canonical_name if the id is ever absent) so a
        platform-wide caller can join against JDSkill.canonical_skill_id.
        Sorted by occurrence count, descending.

        `rejections` are decision_details dicts (see _classify_rejection).
        """
        counts: dict = {}
        for rejection in rejections:
            detail = rejection or {}
            for skill in detail.get("mandatory_skills", []):
                if skill.get("match_type") != "MISSING":
                    continue
                canonical_name = skill.get("canonical_name") or "Unknown skill"
                key = skill.get("canonical_skill_id") or canonical_name
                if key not in counts:
                    counts[key] = {
                        "canonical_skill_id": skill.get("canonical_skill_id"),
                        "canonical_name": canonical_name,
                        "count": 0,
                    }
                counts[key]["count"] += 1

        return sorted(counts.values(), key=lambda entry: entry["count"], reverse=True)

    def _build_top_missing_skills(
        self, rejections: list[dict], total_rejections: int, limit: int,
    ) -> list[MissingSkillOccurrence]:
        aggregated = self._aggregate_missing_skills(rejections)[:limit]
        return [
            MissingSkillOccurrence(
                canonical_name=entry["canonical_name"],
                occurrence_count=entry["count"],
                percentage_of_rejections=(
                    round(entry["count"] / total_rejections * 100, 2) if total_rejections else 0.0
                ),
            )
            for entry in aggregated
        ]

    def _get_min_candidates_for_analytics(self) -> int:
        if self.config_repo is None:
            return _DEFAULT_MIN_CANDIDATES_FOR_ANALYTICS
        configs = self.config_repo.get_configs_by_keys([_MIN_CANDIDATES_FOR_ANALYTICS_KEY])
        raw = configs.get(_MIN_CANDIDATES_FOR_ANALYTICS_KEY)
        return int(raw) if raw is not None else _DEFAULT_MIN_CANDIDATES_FOR_ANALYTICS

    def _build_calibration_recommendations(
        self,
        campaign: HiringCampaign,
        breakdown: list[RejectionBreakdownEntry],
        top_missing_skills: list[MissingSkillOccurrence],
    ) -> list[JdCalibrationRecommendation]:
        recommendations = []

        skill_mismatch_threshold = self._read_config_float(
            _SKILL_MISMATCH_RATE_THRESHOLD_KEY, _DEFAULT_SKILL_MISMATCH_RATE_THRESHOLD,
        )
        for skill in top_missing_skills:
            if skill.percentage_of_rejections > skill_mismatch_threshold:
                recommendations.append(JdCalibrationRecommendation(
                    rule="SKILL_MISMATCH",
                    message=(
                        f"Consider making {skill.canonical_name} preferred rather than "
                        "mandatory, or adding aliases."
                    ),
                    action="review_skill_ontology",
                    details={
                        "skill": skill.canonical_name,
                        "missing_rate": skill.percentage_of_rejections,
                        "threshold": skill_mismatch_threshold,
                    },
                ))

        experience_only_threshold = self._read_config_float(
            _EXPERIENCE_ONLY_RATE_THRESHOLD_KEY, _DEFAULT_EXPERIENCE_ONLY_RATE_THRESHOLD,
        )
        experience_only_entry = next(
            (entry for entry in breakdown if entry.category == _EXPERIENCE_ONLY), None,
        )
        if experience_only_entry is not None and experience_only_entry.percentage > experience_only_threshold:
            current_min_years, current_tolerance = self._current_experience_config(campaign)
            recommendations.append(JdCalibrationRecommendation(
                rule="EXPERIENCE_MISMATCH",
                message="Consider reducing minimum experience or increasing tolerance.",
                action="review_campaign_configuration",
                details={
                    "experience_only_rate": experience_only_entry.percentage,
                    "threshold": experience_only_threshold,
                    "current_min_experience_years": current_min_years,
                    "recommended_min_experience_years": (
                        max(0.0, current_min_years - 1) if current_min_years is not None else None
                    ),
                    "current_experience_tolerance_years": current_tolerance,
                    "recommended_experience_tolerance_years": (
                        current_tolerance + 0.5 if current_tolerance is not None else None
                    ),
                },
            ))

        override_rate, override_threshold = self._compute_rule3_override_rate(campaign.id)
        if override_rate > override_threshold:
            recommendations.append(JdCalibrationRecommendation(
                rule="HIGH_OVERRIDE_RATE",
                message="High override rate detected. Review JD skills or deterministic thresholds.",
                action="review_campaign_configuration",
                details={"override_rate": override_rate, "threshold": override_threshold},
            ))

        return recommendations

    def _read_config_float(self, key: str, default: float) -> float:
        if self.config_repo is None:
            return default
        raw = self.config_repo.get_configs_by_keys([key]).get(key)
        return float(raw) if raw is not None else default

    def _current_experience_config(self, campaign: HiringCampaign) -> tuple[float | None, float | None]:
        current_min_years = None
        job_description = getattr(campaign, "job_description", None)
        if job_description is not None and job_description.min_experience_years is not None:
            current_min_years = float(job_description.min_experience_years)

        current_tolerance = None
        if self.config_repo is not None:
            raw = self.config_repo.get_configs_by_keys([_EXPERIENCE_TOLERANCE_YEARS_KEY]).get(
                _EXPERIENCE_TOLERANCE_YEARS_KEY,
            )
            current_tolerance = float(raw) if raw is not None else None

        return current_min_years, current_tolerance

    def _compute_rule3_override_rate(self, campaign_id: UUID) -> tuple[float, float]:
        """
        Same override_rate formula as the Override Report's per-campaign
        alert (overrides / all-time REJECTED candidates) - deliberately the
        SAME denominator convention as _compute_campaign_alerts (M07-E03
        S04), so this rule and that report's alert always agree.
        """
        threshold = self._read_config_float(
            _OVERRIDE_RATE_ALERT_THRESHOLD_KEY, _DEFAULT_OVERRIDE_RATE_ALERT_THRESHOLD,
        )
        override_count = len(self.campaign_candidate_repo.get_overridden(campaign_id=campaign_id))
        rejected_count = len(self.campaign_candidate_repo.get_rejected_by_campaign(campaign_id))
        override_rate = (override_count / rejected_count * 100) if rejected_count else 0.0
        return round(override_rate, 2), threshold

    # ------------------------------------------------------------------
    # M07-E03 S05 T03: Platform-wide Deterministic Rejection Summary Export
    # ------------------------------------------------------------------

    def export_deterministic_rejection_summary(
        self,
        date_from: datetime | None,
        date_to: datetime | None,
        actor_id: str,
        actor_role: str | None,
    ) -> StreamingResponse:
        """
        Reuses ExcelExport (no new XLSX engine) and AuditService. 3 sheets:
        Campaign Summary, Skill Gap Analysis, Override Log. Never includes
        candidate name/email/phone/resume - only the opaque candidate_uuid
        on the Override Log sheet.
        """
        campaigns = self.campaign_repo.get_all_campaigns(show_closed=True)

        rejection_rows = self.campaign_repo.get_deterministic_rejection_details(
            date_from=date_from, date_to=date_to,
        )
        rejections_by_campaign: dict[UUID, list[dict]] = {}
        for rejection, campaign_id in rejection_rows:
            rejections_by_campaign.setdefault(campaign_id, []).append(rejection)

        campaign_summary_rows = [
            self._to_campaign_summary_row(campaign, rejections_by_campaign.get(campaign.id, []), date_from, date_to)
            for campaign in campaigns
        ]

        all_rejections = [rejection for rejections in rejections_by_campaign.values() for rejection in rejections]
        skill_gap_rows = self._build_skill_gap_rows(all_rejections)

        override_report = self.get_override_report(campaign_id=None, date_from=date_from, date_to=date_to)
        override_log_rows = [self._to_override_log_row(row) for row in override_report.rows]

        excel_file = ExcelExport.export_deterministic_rejection_summary(
            campaign_summary_rows, skill_gap_rows, override_log_rows,
        )
        filename = f"deterministic_rejection_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

        self.audit_service.log(
            actor_id=actor_id,
            actor_role=actor_role,
            action_type=ActionType.DETERMINISTIC_ANALYTICS_EXPORTED,
            entity_type=EntityType.CAMPAIGN_CANDIDATE,
            entity_id=self.EXPORT_AUDIT_ENTITY_ID,
            campaign_id=None,
            details={
                "campaign_count": len(campaign_summary_rows),
                "skill_count": len(skill_gap_rows),
                "override_count": len(override_log_rows),
            },
        )

        return StreamingResponse(
            excel_file,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    def _to_campaign_summary_row(
        self,
        campaign: HiringCampaign,
        campaign_rejections: list[dict],
        date_from: datetime | None,
        date_to: datetime | None,
    ) -> dict:
        total_candidates = self.campaign_candidate_repo.get_candidate_count(campaign.id)
        deterministic_rejections = len(campaign_rejections)
        rejection_rate = (
            round(deterministic_rejections / total_candidates * 100, 2) if total_candidates else 0.0
        )

        category_counts: dict[str, int] = {}
        for rejection in campaign_rejections:
            category = self._classify_rejection(rejection)
            if category is None:
                continue
            category_counts[category] = category_counts.get(category, 0) + 1
        top_category = max(category_counts, key=category_counts.get) if category_counts else None
        top_rejection_reason = _BREAKDOWN_CATEGORY_DISPLAY.get(top_category, "") if top_category else ""

        # Date-scoped together with deterministic_rejections above, so this
        # column stays internally consistent with the rest of THIS report's
        # row - a deliberately different denominator convention from
        # _compute_rule3_override_rate's all-time one, since that rule must
        # instead match the already-shipped Override Report's alert exactly.
        override_count = len(
            self.campaign_candidate_repo.get_overridden(campaign_id=campaign.id, date_from=date_from, date_to=date_to)
        )
        override_rate = (
            round(override_count / deterministic_rejections * 100, 2) if deterministic_rejections else 0.0
        )

        return {
            "campaign_name": campaign.name,
            "total_candidates": total_candidates,
            "deterministic_rejections": deterministic_rejections,
            "rejection_rate": rejection_rate,
            "top_rejection_reason": top_rejection_reason,
            "override_count": override_count,
            "override_rate": override_rate,
        }

    def _build_skill_gap_rows(self, all_rejections: list[dict]) -> list[dict]:
        aggregated = self._aggregate_missing_skills(all_rejections)
        total_rejections = len(all_rejections)

        skill_ids = [entry["canonical_skill_id"] for entry in aggregated if entry["canonical_skill_id"]]
        requirement_counts = (
            self.skill_repo.get_campaign_requirement_counts_by_skill(skill_ids)
            if self.skill_repo is not None else {}
        )

        return [
            {
                "canonical_name": entry["canonical_name"],
                "campaigns_requiring_skill": requirement_counts.get(entry["canonical_skill_id"], 0),
                "missing_count": entry["count"],
                "missing_rate": (
                    round(entry["count"] / total_rejections * 100, 2) if total_rejections else 0.0
                ),
            }
            for entry in aggregated
        ]

    @staticmethod
    def _to_override_log_row(row: OverrideReportRow) -> dict:
        return {
            "campaign_name": row.campaign_name,
            "candidate_uuid": str(row.candidate_uuid),
            "rejection_reason": row.original_rejection_reason or "",
            "override_reason": row.override_reason,
            "override_by": row.hr_full_name or "",
            "override_timestamp": row.override_timestamp,
            "current_pipeline_stage": row.current_pipeline_stage.value,
        }

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