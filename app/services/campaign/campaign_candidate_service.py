import hashlib
import logging
from uuid import UUID, uuid4

from datetime import datetime

from fastapi.responses import StreamingResponse

from app.services.campaign_access import acting_user, can_access_campaign
from app.core.encryption_service import DecryptionError, EncryptionService
from app.core.storage_service import StorageService
from app.enums.constants import ActionType, DEFAULT_MIN_CANDIDATES_FOR_ANALYTICS, DEFAULT_PAGE_SIZE, EntityType, MAX_PAGE_SIZE
from app.exception_handler.exceptions import NotFoundError
from app.exceptions.campaign_exceptions import CampaignException
from app.exceptions.pipeline_transition_exceptions import (
    ForbiddenPipelineRoleException,
    InvalidPipelineTransitionException,
    PipelineStageConflictException,
    PipelineTransitionReasonRequiredException,
)
from app.models.campaigns import CampaignStatus, HiringCampaign
from app.models.email import EmailTriggerEvent
from app.models.candidates import Candidate, FileFormat, ParseStatus, Resume
from app.models.pipeline import (
    AIEvaluationStatus,
    AIRecommendation,
    CampaignCandidate,
    DecisionSource,
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
from app.repositories.candidate_note_repository import CandidateNoteRepository
from app.repositories.candidate_repository import CandidateRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.dead_letter_queue_repository import DeadLetterQueueRepository
from app.repositories.email_notification_repository import EmailNotificationRepository
from app.repositories.email_template_repository import EmailTemplateRepository
from app.repositories.interview_schedule_repository import InterviewScheduleRepository
from app.repositories.resume_repository import ResumeRepository
from app.repositories.skill_repository import SkillRepository
from app.services.campaign.scoring_v2_service import v2_rejection_reason
from app.services.campaign.v2_breakdown import (
    as_v2, skills_check_failed, unconfirmed_core_entries, unconfirmed_core_skills,
)
from app.schemas.campaign.campaign_candidate_schema import (
    AiSummaryDetail,
    DeterministicDecisionCheck,
    CampaignCandidateCreateRequest,
    CampaignBoardColumn,
    CampaignBoardResponse,
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
    PreferredSkillBreakdownItem,
    RejectionBreakdownEntry,
    ScoreCalculationDetail,
    ScoreConfigurationDetail,
    CampaignCandidateSummaryResponse,
    CandidateCompositeScoreHistoryEntryResponse,
    CandidateCompositeScoreHistoryResponse,
    CandidateCompositeResponse,
    CandidateRankingDetailsResponse,
    CandidateSemanticResponse,
    CandidateTimelineEventResponse,
    CandidateTimelineResponse,
    RankedCampaignCandidatesResponse,
    SemanticScoreBreakdownResponse,
    SemanticScoreSummary,
    BulkSendRejectionEmailResponse,
    ResubmissionInfoResponse,
    SendRejectionEmailResponse,
    SendSelectionEmailResponse,
    UpdateResumeResubmissionResponse,
)
from app.services.audit_service import AuditService
from app.schemas.campaign.layer_failure_schema import LayerFailureResponse
from app.services.campaign.layer_failure import (
    LAYER_AI,
    LAYER_DETERMINISTIC,
    LAYER_SEMANTIC,
    get_layer_failures,
)
from app.services.campaign.outcome_override_report import campaign_override_rate
from app.services.notifications.candidate_notification_emails import queue_candidate_selected_email
from app.services.notifications.candidate_rejection_email_service import CandidateRejectionEmailService
from app.services.campaign.pipeline_transition_service import PipelineTransitionService
from app.services.campaign.stage_transition_service import Actor, StageTransitionService
from app.services.celery_task_log_service import CeleryTaskLogService
from app.tasks.email_tasks import send_candidate_email_task
from app.services.resume.file_validation_service import FileValidationService
from app.tasks.resume_processing_tasks import process_resume_document
from app.utils.excel_export import ExcelExport
from app.services.candidate_change_notifier import CandidateChangeNotifier

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

# M07-E03 S04 T02: task_type string this service queues after an override,
# when re-enqueuing semantic scoring rather than AI evaluation (the latter
# now goes through ai_evaluation_tasks._enqueue_ai_evaluation directly,
# which owns its own AI_EVALUATE_TASK_TYPE constant).
SEMANTIC_SCORE_TASK_TYPE = "SEMANTIC_SCORE"

_HR_OVERRIDE_CHANGE_REASON = "RECRUITER override of deterministic rejection"
_OVERRIDE_RATE_ALERT_THRESHOLD_KEY = "OVERRIDE_RATE_ALERT_THRESHOLD"
_DEFAULT_OVERRIDE_RATE_ALERT_THRESHOLD = 20.0

# M07-E03 S05: Deterministic Rejection Analytics
_MIN_CANDIDATES_FOR_ANALYTICS_KEY = "MIN_CANDIDATES_FOR_ANALYTICS"
# Reuses the exact same platform_config key deterministic_scoring_tasks.py
# already reads for scoring (never a second, S05-local key).

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

# Match-type values that count as a hierarchy fallback (not EXACT, not MISSING).
_HIERARCHY_RELATIONSHIP_MATCH_TYPES = {"CHILD", "GRANDCHILD", "SIBLING", "SEMANTIC"}

_SKILL_MATCH_TIER_LABELS = {
    "CHILD": "a related child skill",
    "GRANDCHILD": "a related grandchild skill",
    "SIBLING": "a related sibling skill",
    "SEMANTIC": "a semantically similar skill",
}


# Why a core skill that was found still didn't count toward the core gate (v2 confirmation).
_UNCONFIRMED_CORE_REASONS = {
    "REJECTED": "Found, but the review found it isn't shown in the candidate's work.",
    "UNCERTAIN": "Found, but not confirmed as core (listed only, or the votes split).",
    "UNCONFIRMED": "Found, but not confirmed as core.",
    "NOT_RUN": "Found, not confirmed yet - confirmation didn't run.",
    "SENSE_MISMATCH": "Found in a different sense than the JD means.",
    None: "Found, but not confirmed as core.",
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
        email_notification_repo: EmailNotificationRepository | None = None,
        candidate_note_repo: CandidateNoteRepository | None = None,
        interview_schedule_repo: InterviewScheduleRepository | None = None,
        dead_letter_queue_repo: DeadLetterQueueRepository | None = None,
        celery_task_log_repo: CeleryTaskLogRepository | None = None,
        upload_decision_service=None,
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
        # Bug fix: delete_campaign_candidate used to call
        # campaign_candidate_repo.delete() directly with none of these,
        # which raised a raw ForeignKeyViolation the moment any of these
        # tables held a row for that campaign_candidate (stage history,
        # any transition at all, a note, an interview round, a queued/
        # completed processing task, a dead-lettered failure) - i.e. any
        # candidate that wasn't still sitting untouched at UPLOADED.
        # Optional + lazily defaulted from campaign_candidate_repo.db, same
        # convention as composite_score_history_repo above, so no existing
        # call site needs to change.
        self.email_notification_repo = email_notification_repo or EmailNotificationRepository(
            campaign_candidate_repo.db,
        )
        self.candidate_note_repo = candidate_note_repo or CandidateNoteRepository(campaign_candidate_repo.db)
        self.interview_schedule_repo = interview_schedule_repo or InterviewScheduleRepository(
            campaign_candidate_repo.db,
        )
        self.dead_letter_queue_repo = dead_letter_queue_repo or DeadLetterQueueRepository(
            campaign_candidate_repo.db,
        )
        self.celery_task_log_repo = celery_task_log_repo or CeleryTaskLogRepository(campaign_candidate_repo.db)
        # Duplicate resume decisions (UploadDecisionService): optional, no
        # default - only the DI provider supplies it (delete_campaign_candidate
        # closes the removed candidate's pending decisions).
        self.upload_decision_service = upload_decision_service

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

    def add_campaign_candidate(
        self, request, actor_id: str | None, actor_role: str | None = None,
        actor_roles: list[str] | None = None,
    ) -> CampaignCandidateResponse:
        """
        create_campaign_candidate + commit, for callers that own no wider
        transaction (the direct POST /campaign-candidates route). The response
        is returned only after the commit succeeded.

        actor_roles (from the route): only an HR_ADMIN, or a recruiter whose
        campaign it is (assigned recruiter or creator), may add a candidate.
        """
        from app.repositories.recruiter_campaigns import is_recruiters_campaign
        if actor_roles is not None and "HR_ADMIN" not in actor_roles:
            campaign = self.campaign_repo.get_by_id(request.campaign_id)
            # a missing campaign falls through to create_campaign_candidate's own 404
            if campaign is not None and not ("RECRUITER" in actor_roles
                                             and is_recruiters_campaign(campaign, actor_id)):
                raise CampaignException(
                    "Only an HR admin or the campaign's recruiter can add candidates to it.", 403,
                )
        response = self.create_campaign_candidate(request, actor_id=actor_id, actor_role=actor_role)
        try:
            self.campaign_candidate_repo.commit()
        except Exception:
            self.campaign_candidate_repo.rollback()
            raise
        return response

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
        actor_roles: list[str] | None = None,
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
            # Any of the caller's roles that the -> UPLOADED transition permits
            # authorises it; that role is what the history and audit record.
            if actor_roles:
                actor_role = self.pipeline_transition_service.permitted_role(
                    campaign_candidate, PipelineStage.UPLOADED, actor_roles,
                )
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
        except ForbiddenPipelineRoleException as exc:
            self.campaign_candidate_repo.rollback()
            raise CampaignException(str(exc), 403) from exc
        except PipelineStageConflictException as exc:
            self.campaign_candidate_repo.rollback()
            raise CampaignException(str(exc), 409) from exc

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

        CandidateChangeNotifier().stage_changed(campaign_candidate.campaign_id, campaign_candidate)

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

    # Pipeline Board's 8 columns - FRAUD_REVIEW isn't part of this board (a
    # candidate can still be in it; see other_count). HM_REVIEW sits between
    # HOLD and INTERVIEW, matching where it falls in the pipeline flow.
    _BOARD_STAGES = (
        PipelineStage.UPLOADED,
        PipelineStage.SCREENING,
        PipelineStage.SHORTLISTED,
        PipelineStage.HOLD,
        PipelineStage.HM_REVIEW,
        PipelineStage.INTERVIEW,
        PipelineStage.SELECTED,
        PipelineStage.REJECTED,
    )

    def get_campaign_board(self, campaign_id: UUID) -> CampaignBoardResponse:
        """
        Pipeline Board - the exact same enriched candidate rows
        get_campaign_candidates already returns for the Candidate Listing
        UI, grouped by pipeline_stage into board columns. No new query and
        no new per-row mapping: this only buckets get_campaign_candidates'
        own output. A "Parsing" column (parse_status, not a pipeline_stage)
        is a frontend-only split of the UPLOADED column's items by their
        parse_status field, already present on each item.
        """
        items = self.get_campaign_candidates(campaign_id)

        by_stage: dict[PipelineStage, list[CampaignCandidateResponse]] = {
            stage: [] for stage in self._BOARD_STAGES
        }
        other_count = 0
        for item in items:
            if item.pipeline_stage in by_stage:
                by_stage[item.pipeline_stage].append(item)
            else:
                other_count += 1

        columns = [
            CampaignBoardColumn(stage=stage, count=len(by_stage[stage]), candidates=by_stage[stage])
            for stage in self._BOARD_STAGES
        ]

        return CampaignBoardResponse(campaign_id=campaign_id, columns=columns, other_count=other_count)

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
        candidate_name: str | None = None,
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
            candidate_name=candidate_name,
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
        ai_evaluation_status = (
            getattr(ai_evaluation, "ai_evaluation_status", None)
            if ai_evaluation else getattr(campaign_candidate, "ai_evaluation_status", None)
        )
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
                if ai_evaluation and ai_evaluation.effective_ai_score is not None
                else (
                    float(campaign_candidate.effective_ai_score)
                    if getattr(campaign_candidate, "effective_ai_score", None) is not None else None
                )
            ),
            weight_deterministic=float(campaign.weight_deterministic),
            weight_semantic=float(campaign.weight_semantic),
            weight_ai=float(campaign.weight_ai),
            formula_version=formula_version,
            ranking_status=self._derive_ranking_status(campaign_candidate),
            composite_score_computed_at=getattr(campaign_candidate, "composite_score_computed_at", None),
        )

    def get_candidate_composite(self, campaign_candidate_id: UUID) -> CandidateCompositeResponse:
        """
        Composite-tab-only view: same stored score inputs and current campaign
        weights used by ranking-details, without HR override/final-status data.
        Never recalculates composite_score.
        """
        details = self.get_candidate_ranking_details(campaign_candidate_id)
        return CandidateCompositeResponse(
            campaign_candidate_id=details.campaign_candidate_id,
            composite_score=details.composite_score,
            deterministic_score=details.deterministic_score,
            semantic_score=details.semantic_score,
            ai_evaluation_score=details.ai_evaluation_score,
            weight_deterministic=details.weight_deterministic,
            weight_semantic=details.weight_semantic,
            weight_ai=details.weight_ai,
            formula_version=details.formula_version,
            ranking_status=details.ranking_status,
            composite_score_computed_at=details.composite_score_computed_at,
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
            # ai_ats_score reads from effective_ai_score - the AI evaluation's
            # own ai_ats_score column is never written by any code path
            # (AIEvaluationService only ever sets effective_ai_score), so
            # reading it directly here always returned null regardless of
            # whether AI evaluation actually ran.
            ai_ats_score=(
                float(ai_evaluation.effective_ai_score)
                if ai_evaluation and ai_evaluation.effective_ai_score is not None else None
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
            ai_recommendation=ai_evaluation.ai_recommendation if ai_evaluation else None,
            decision_type=getattr(campaign_candidate, "decision_type", None),
            decision_source=getattr(campaign_candidate, "decision_source", None),
            decision_reason=getattr(campaign_candidate, "decision_reason", None),
            decision_at=getattr(campaign_candidate, "decision_at", None),
            awaiting_full_scoring=(
                (getattr(campaign_candidate, "deterministic_breakdown", None) or {}).get("awaiting_full_scoring") is True
                if isinstance(getattr(campaign_candidate, "deterministic_breakdown", None), dict) else False
            ),
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

    def get_candidate_deterministic(
        self, campaign_candidate_id: UUID, include_shadow: bool = False,
    ) -> CandidateDeterministicResponse:
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
            failure=self._layer_failure(campaign_candidate, LAYER_DETERMINISTIC),
            scoring_v2_shadow=self._scoring_v2_shadow(campaign_candidate) if include_shadow else None,
        )

    def _scoring_v2_shadow(self, campaign_candidate) -> dict | None:
        """The core-skill detail of the latest acted-on result (HR_ADMIN panel, read only). Not latest(): a
        candidate scoring version's shadow row can be newer, and it is never the candidate's result."""
        try:
            from app.repositories.scoring_result_repository import ScoringResultRepository
            from app.services.campaign.scoring_shadow_report_service import outcome_label
            row = ScoringResultRepository(self.campaign_candidate_repo.db).latest_acted_on(campaign_candidate.id)
            if row is None:
                return None
            b = row.breakdown or {}
            confirmation = {r.get("canonical_skill_id"): r for r in (b.get("confirmation") or {}).get("results") or []}
            core = []
            for entry in b.get("mandatory_skills") or []:
                if entry.get("importance") != "core":
                    continue
                result = confirmation.get(entry.get("canonical_skill_id")) or {}
                core.append({
                    "name": entry.get("canonical_name"), "status": entry.get("confirmation_status"),
                    "match_type": entry.get("match_type"), "counts_for_core_gate": entry.get("counts_for_core_gate"),
                    "evidence_sources": entry.get("evidence_sources") or [], "evidence_text": entry.get("evidence_text"),
                    "jd_sense": entry.get("jd_sense"), "jd_sense_basis": entry.get("jd_sense_basis"),
                    "quote": result.get("quote"), "votes": result.get("votes") or [], "method": result.get("method"),
                })
            return {
                "outcome": outcome_label(row), "outcome_reason": b.get("outcome_reason"),
                "score": float(row.score) if row.score is not None else None,
                "provisional_score": b.get("provisional_score"),
                "core_confirmed_count": b.get("core_confirmed_count"), "core_skill_count": b.get("core_skill_count"),
                "allowed_missing_core_skills": b.get("allowed_missing_core_skills"),
                "core_found_not_confirmed": b.get("core_found_not_confirmed") or [],
                "core_uncertain": b.get("core_uncertain") or [],
                "core_rejected_by_llm": b.get("core_rejected_by_llm") or [],
                "core_sense_mismatch": b.get("core_sense_mismatch") or [],
                "failed_checks": b.get("failed_checks") or [], "core_skills": core,
                "components": b.get("components") or [], "scored_at": row.created_at.isoformat() if row.created_at else None,
                "scoring_config_version": row.scoring_config_version,
            }
        except Exception:
            logger.exception("Could not build the core-skill panel | campaign_candidate_id=%s", campaign_candidate.id)
            return None

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
            failure=self._layer_failure(campaign_candidate, LAYER_SEMANTIC),
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
            failure=self._layer_failure(campaign_candidate, LAYER_AI),
        )

    def _layer_failure(self, campaign_candidate: CampaignCandidate, layer: str) -> LayerFailureResponse | None:
        """This layer's open dead-lettered task, if it stopped there - additive, best-effort."""
        return get_layer_failures(self.dead_letter_queue_repo, campaign_candidate).get(layer)

    @staticmethod
    def _build_semantic_score_breakdown(
        campaign_candidate: CampaignCandidate,
    ) -> SemanticScoreBreakdownResponse | None:
        breakdown = campaign_candidate.semantic_breakdown
        if not breakdown:
            # Bug fix: a candidate rejected at DETERMINISTIC never has
            # semantic scoring enqueued at all (deterministic_scoring_tasks.py
            # only calls _enqueue_semantic_scoring on a deterministic PASS),
            # so semantic_breakdown stays NULL forever - indistinguishable
            # from "hasn't reached semantic yet" without this check. Scoped
            # to REJECTED + DETERMINISTIC only, matching
            # _SCORECARD_BANNER_DECISION_SOURCE's existing scope - an
            # overridden-past-rejection candidate is a separate, narrower
            # case left alone here.
            if (
                campaign_candidate.pipeline_stage == PipelineStage.REJECTED
                and campaign_candidate.decision_source == DecisionSource.DETERMINISTIC
            ):
                return SemanticScoreBreakdownResponse(summary=SemanticScoreSummary(status="NOT_APPLICABLE"))
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

    def _build_rejection_banner(self, campaign_candidate: CampaignCandidate) -> dict:
        has_rejection = False
        rejection_layer = None
        rejection_reason = None
        rejected_at = None

        # Currently REJECTED: the candidate's own decision_* fields ARE the latest rejection.
        if campaign_candidate.pipeline_stage == PipelineStage.REJECTED:
            if campaign_candidate.decision_source == _SCORECARD_BANNER_DECISION_SOURCE:
                has_rejection = True
                rejection_layer = campaign_candidate.decision_source
                rejection_reason = campaign_candidate.decision_reason
                rejected_at = campaign_candidate.decision_at

        return {
            "has_rejection": has_rejection,
            "rejection_layer": rejection_layer,
            "rejection_reason": rejection_reason,
            "rejected_at": rejected_at,
            "score_breakdown": campaign_candidate.deterministic_breakdown,
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
        """
        The Deterministic tab, from v2's stored breakdown (retire v1, phase C - as_v2 also reads a
        pre-phase-C v1-shaped one). Pure read/transform of what scoring stored; nothing recalculated.
        """
        breakdown = as_v2(campaign_candidate.deterministic_breakdown)
        if not breakdown:
            return None

        mandatory_raw = breakdown.get("mandatory_skills") or []
        preferred_raw = breakdown.get("preferred_skills") or []
        experience = breakdown.get("experience_validation")
        education = breakdown.get("education_validation")
        domain = breakdown.get("domain_capability_validation")

        def found(entry):
            return entry.get("match_type") != "MISSING"

        def counts(entry):
            # A core skill counts only when confirmed (the core gate); any other one when found.
            return bool(entry.get("counts_for_core_gate")) if entry.get("importance") == "core" else found(entry)

        def percentage(entry):
            weight = entry.get("effective_weight")
            return round(entry["contribution"] / weight * 100, 2) if weight and entry.get("contribution") is not None else None

        mandatory_skills = [
            MandatorySkillBreakdownItem(
                jd_skill=entry.get("canonical_name"),
                candidate_skill=entry.get("matched_skill_name") if found(entry) else None,
                mandatory=entry.get("mandatory"),
                importance=entry.get("importance"),
                match_type=entry.get("match_type"),
                matched_via_alias=False,
                configured_weight=entry.get("configured_weight"),
                normalization_discount=entry.get("recognition"),
                hierarchy_multiplier=entry.get("hierarchy_multiplier"),
                contribution=entry.get("contribution"),
                confidence=entry.get("recognition"),
                passed=counts(entry),
                matched=found(entry),
                match_reason=self._skill_match_reason(entry.get("match_type"), entry.get("matched_skill_name")),
                contribution_percentage=percentage(entry),
                confirmation_status=entry.get("confirmation_status"),
                counts_for_core_gate=entry.get("counts_for_core_gate"),
            )
            for entry in mandatory_raw
        ]
        preferred_skills = [
            PreferredSkillBreakdownItem(
                jd_skill=entry.get("canonical_name"),
                candidate_skill=entry.get("matched_skill_name") if found(entry) else None,
                match_type=entry.get("match_type"),
                configured_weight=entry.get("configured_weight"),
                bonus=entry.get("contribution"),
                confidence=entry.get("recognition"),
                matched=found(entry),
                match_reason=self._skill_match_reason(entry.get("match_type"), entry.get("matched_skill_name")),
                contribution_percentage=percentage(entry),
            )
            for entry in preferred_raw
        ]
        missing_mandatory_skills = [
            MissingMandatorySkillItem(
                skill=entry.get("canonical_name"),
                configured_weight=entry.get("configured_weight"),
                importance=entry.get("importance"),
                reason=(self._missing_skill_reason(entry.get("importance")) if not found(entry)
                        else _UNCONFIRMED_CORE_REASONS.get(entry.get("confirmation_status"),
                                                           _UNCONFIRMED_CORE_REASONS[None])),
            )
            for entry in mandatory_raw if not counts(entry)
        ]
        hierarchy_matches = [
            HierarchyMatchItem(
                jd_skill=entry.get("canonical_name"),
                candidate_skill=entry.get("matched_skill_name"),
                relationship=entry.get("match_type"),
                multiplier=entry.get("hierarchy_multiplier"),
                match_type=entry.get("match_type"),
                hierarchy_multiplier=entry.get("hierarchy_multiplier"),
            )
            for entry in (mandatory_raw + preferred_raw)
            if entry.get("match_type") in _HIERARCHY_RELATIONSHIP_MATCH_TYPES
        ]

        mandatory_matched = sum(1 for entry in mandatory_raw if found(entry))
        weights = {c.get("name"): c.get("weight") for c in breakdown.get("components") or []}
        experience_min_years = experience.get("min_years") if experience else None
        core_total = breakdown.get("core_skill_count")

        from app.services.campaign.score_display import _degree_level_display

        passed = breakdown.get("deterministic_passed")
        failure_reason = None if passed else v2_rejection_reason(breakdown)
        failure_reasons = failure_reason.split(" | ") if failure_reason else []

        return DeterministicScoreBreakdownResponse(
            summary=DeterministicScoreSummary(
                overall_score=breakdown.get("deterministic_score"),
                status="PASSED" if passed else "FAILED",
                threshold=breakdown.get("deterministic_threshold"),
                mandatory_coverage_pct=(round(mandatory_matched / len(mandatory_raw) * 100, 2)
                                        if mandatory_raw else None),
                core_coverage_pct=breakdown.get("core_coverage_pct"),
                gate_skill_scope="core" if core_total else "mandatory",
                missing_core_skill_count=breakdown.get("missing_core_skill_count"),
                mandatory_skills_matched=mandatory_matched,
                mandatory_skills_total=len(mandatory_raw),
                core_skills_matched=breakdown.get("core_confirmed_count") if core_total else None,
                core_skills_total=core_total or None,
                preferred_skills_matched=sum(1 for entry in preferred_raw if found(entry)),
                preferred_skills_total=len(preferred_raw),
                additional_skills_count=None,
                experience_status=self._validation_status(experience),
                education_status=self._validation_status(education),
                screened_at=getattr(campaign_candidate, "screened_at", None),
                decision_checks=[
                    DeterministicDecisionCheck(check=c["check"], passed=bool(c.get("passed")),
                                               can_reject=bool(c.get("applies")), detail=c.get("detail") or "")
                    for c in breakdown.get("checks") or []
                ],
                failure_reason=failure_reason,
                failure_reasons=failure_reasons,
                screening_completed_at=getattr(campaign_candidate, "screened_at", None),
                outcome=breakdown.get("outcome"),
                scoring_mode=breakdown.get("scoring_mode"),
                awaiting_full_scoring=bool(breakdown.get("awaiting_full_scoring")),
                needs_review_reason=breakdown.get("needs_review_reason") if breakdown.get("needs_review") else None,
            ),
            missing_mandatory_skills=missing_mandatory_skills,
            mandatory_skills=mandatory_skills,
            preferred_skills=preferred_skills,
            additional_candidate_skills=[],
            hierarchy_matches=hierarchy_matches,
            experience_validation=ExperienceValidationDetail(
                required_years=experience_min_years,
                candidate_years=experience.get("candidate_years") if experience else None,
                tolerance=experience.get("near_miss_tolerance_years") if experience else None,
                passed=experience.get("passed") if experience else None,
                status=self._detailed_validation_status(experience),
            ),
            education_validation=EducationValidationDetail(
                required_degree=(
                    (education.get("required_degree_text") or (
                        _degree_level_display(education.get("required_level"))
                        if education.get("required_level") else None
                    ))
                    if education and not education.get("skipped") else None
                ),
                candidate_degree=(
                    (education.get("candidate_degree_text") or _degree_level_display(education.get("candidate_level")))
                    if education else None
                ),
                equivalent_experience_applied=education.get("equivalent_experience_applied") if education else None,
                passed=education.get("passed") if education else None,
                status=self._validation_status(education),
                **self._education_field_detail(education),
            ),
            score_calculation=ScoreCalculationDetail(
                skills_score=breakdown.get("skill_score"),
                functional_score=breakdown.get("functional_score"),
                experience_score=experience.get("score") if experience else None,
                education_score=education.get("score") if education else None,
                final_score=breakdown.get("deterministic_score"),
                functional_applicable=breakdown.get("functional_score") is not None,
                experience_applicable=bool(experience.get("applicable")) if experience else None,
                education_applicable=bool(education.get("applicable")) if education else None,
                required_domain_capabilities=(domain or {}).get("required") or [],
                preferred_domain_capabilities=(domain or {}).get("preferred") or [],
            ),
            configuration=ScoreConfigurationDetail(
                # v2's component weights, as this score used them (experience and education
                # are checks in v2, not weighted components).
                skills_weight=weights.get("skills"),
                functional_weight=weights.get("functional"),
                experience_weight=None,
                education_weight=weights.get("education_preferred"),
                deterministic_threshold=breakdown.get("deterministic_threshold"),
                max_missing_core_skills=breakdown.get("configured_allowed_missing_core_skills"),
                allowed_missing_core_skills=breakdown.get("allowed_missing_core_skills"),
            ),
        )

    @staticmethod
    def _missing_skill_reason(importance: str | None) -> str:
        if importance == "core":
            return "Core skill not found - counts toward the missing core-skill limit."
        if importance == "supporting":
            return "Supporting skill not found - lowers the score only."
        return "No matching skill found in the candidate's profile (including related skills)."

    @staticmethod
    def _education_field_detail(education: dict | None) -> dict:
        """Field-of-study half of EducationValidationDetail - empty for pre-field-matching breakdowns."""
        if not education or "field_match" not in education:
            return {}
        from app.services.campaign.score_display import _field_display
        return {
            "required_field": (
                _field_display(education.get("required_field"), education.get("required_field_text"))
                if education.get("required_field") or education.get("required_field_text") else None
            ),
            "candidate_field": (
                _field_display(education.get("candidate_field"), education.get("candidate_field_text"))
                if education.get("candidate_field") or education.get("candidate_field_text") else None
            ),
            "field_match": education.get("field_match"),
            "related_field_allowed": education.get("related_field_allowed"),
            "degree_passed": education.get("level_passed"),
            "field_passed": education.get("field_passed"),
        }

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

        entries = []
        for index, row in enumerate(rejection_rows):
            snapshot = row.scores_snapshot or {}
            decision_source = snapshot.get("decision_source")
            entries.append(CandidateRejectionHistoryEntryResponse(
                id=row.id,
                rejection_layer=DecisionSource(decision_source) if decision_source else DecisionSource.SYSTEM,
                rejection_reason=snapshot.get("decision_reason") or "",
                rejected_at=row.changed_at,
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
        StreamingResponse convention as export_rejected_candidates. Never includes candidate name/email/phone/
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

        # AuditRepository.create only flushes: commit, or the row is rolled back
        # when the request session closes (nothing else commits it).
        self.campaign_candidate_repo.commit()

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

        # AuditRepository.create only flushes: commit, or the row is rolled back
        # when the request session closes (nothing else commits it).
        self.campaign_candidate_repo.commit()

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
        get_rejected_by_campaign), so decision_* IS the current rejection.
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
        }

    @staticmethod
    def _missing_mandatory_skills_display(breakdown: dict) -> str:
        # v2 (phase C): every core skill that didn't count toward the core gate.
        return ", ".join(unconfirmed_core_skills(breakdown))

    @staticmethod
    def _experience_gap_display(breakdown: dict) -> str:
        experience_result = as_v2(breakdown).get("experience_validation")
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
        from app.services.campaign.score_display import describe_education_gap
        level_gap, field_gap = describe_education_gap(as_v2(breakdown).get("education_validation"))
        if field_gap:
            field_gap = f"field of study: {field_gap}"
        return "; ".join(gap for gap in (level_gap, field_gap) if gap)

    # ------------------------------------------------------------------
    # M07-E03 S04 T01/T02: HR_ADMIN Override of a Deterministic Rejection
    # ------------------------------------------------------------------

    # Mirrors JDService.EXPORT_AUDIT_ENTITY_ID / BulkUploadService's
    # EXPORT_AUDIT_ENTITY_ID exactly - a sentinel entity_id for an export
    # that isn't scoped to a single entity (audit_log.entity_id is
    # NOT NULL).
    EXPORT_AUDIT_ENTITY_ID = UUID("00000000-0000-0000-0000-000000000000")

    def _assert_hiring_manager_owns_campaign(
        self, campaign_candidate: CampaignCandidate, user_id: str,
    ) -> None:
        """
        Epic 1: single-resource ownership check - only list-scoping (WHERE
        hiring_manager_id = caller, e.g. get_campaigns_by_hiring_manager)
        existed anywhere in this codebase before. Only ever called for a
        caller without HR_ADMIN among their roles - HR_ADMIN is exempt from
        ownership entirely, regardless of what other roles they also hold.
        """
        campaign = self.campaign_repo.get_by_id(campaign_candidate.campaign_id)
        if not campaign:
            raise CampaignException("Campaign not found.", 404)
        if not can_access_campaign(acting_user(user_id, ["HIRING_MANAGER"]), campaign):
            raise CampaignException(
                "You do not have access to this candidate's campaign.", 403,
            )

    def _transition_or_raise(
        self,
        campaign_candidate_id: UUID,
        to_stage: PipelineStage,
        actor: Actor,
        reason: str | None = None,
    ) -> None:
        """
        Epic 1: shared wrapper around StageTransitionService.transition() -
        the exception -> CampaignException mapping mirrors
        update_resume_for_resubmission's identical mapping above verbatim;
        factored out here since Epic 1 needs it 3 times, not duplicated per
        method.
        """
        try:
            campaign_candidate, changed = self.stage_transition_service.transition(
                campaign_candidate_id=campaign_candidate_id,
                to_stage=to_stage,
                actor=actor,
                reason=reason,
            )
            if changed:
                CandidateChangeNotifier().stage_changed(campaign_candidate.campaign_id, campaign_candidate)
        except InvalidPipelineTransitionException as exc:
            self.campaign_candidate_repo.rollback()
            raise CampaignException(str(exc), 409) from exc
        except PipelineTransitionReasonRequiredException as exc:
            self.campaign_candidate_repo.rollback()
            raise CampaignException(str(exc), 400) from exc
        except ForbiddenPipelineRoleException as exc:
            self.campaign_candidate_repo.rollback()
            raise CampaignException(str(exc), 403) from exc
        except PipelineStageConflictException as exc:
            self.campaign_candidate_repo.rollback()
            raise CampaignException(str(exc), 409) from exc

    def advance_to_interview(
        self,
        campaign_candidate_id: UUID,
        actor_id: str,
        actor_roles: list[str],
    ) -> CandidateScorecardResponse:
        """
        Epic 1: HM_REVIEW -> INTERVIEW. HIRING_MANAGER (own campaign only)
        or HR_ADMIN - allowed_transitions lists both for this edge, no
        reason required (requires_reason=False).
        """
        campaign_candidate = self.campaign_candidate_repo.get_by_id(campaign_candidate_id)
        if campaign_candidate is None:
            raise CampaignException("Campaign candidate not found.", 404)

        if "HR_ADMIN" not in actor_roles:
            self._assert_hiring_manager_owns_campaign(campaign_candidate, actor_id)

        if self.stage_transition_service is None:
            raise CampaignException("Stage transition service is not configured.", 500)

        self._transition_or_raise(
            campaign_candidate_id=campaign_candidate_id,
            to_stage=PipelineStage.INTERVIEW,
            actor=Actor(roles=actor_roles, id=actor_id),
        )

        return self.get_campaign_candidate_scorecard(campaign_candidate_id)

    def select_candidate(
        self,
        campaign_candidate_id: UUID,
        actor_id: str,
        actor_roles: list[str],
    ) -> CandidateScorecardResponse:
        """
        Epic 1: INTERVIEW -> SELECTED. HIRING_MANAGER (own campaign only) or
        HR_ADMIN - allowed_transitions lists both for this edge, no reason
        required (requires_reason=False).
        """
        campaign_candidate = self.campaign_candidate_repo.get_by_id(campaign_candidate_id)
        if campaign_candidate is None:
            raise CampaignException("Campaign candidate not found.", 404)

        if "HR_ADMIN" not in actor_roles:
            self._assert_hiring_manager_owns_campaign(campaign_candidate, actor_id)

        if self.stage_transition_service is None:
            raise CampaignException("Stage transition service is not configured.", 500)

        self._transition_or_raise(
            campaign_candidate_id=campaign_candidate_id,
            to_stage=PipelineStage.SELECTED,
            actor=Actor(roles=actor_roles, id=actor_id),
        )

        return self.get_campaign_candidate_scorecard(campaign_candidate_id)

    def reject_at_interview(
        self,
        campaign_candidate_id: UUID,
        decision_reason: str,
        actor_id: str,
        actor_roles: list[str],
    ) -> CandidateScorecardResponse:
        """
        Epic 1: INTERVIEW -> REJECTED. allowed_transitions restricts this
        edge to HIRING_MANAGER only (HR_ADMIN is not listed, and unlike
        stalled-candidate override there's no HR_ADMIN path to this edge at
        all - _OVERRIDE_FORBIDDEN_TARGETS in campaign_service.py excludes
        REJECTED). The route admits both HIRING_MANAGER and HR_ADMIN
        (matching the other 2 endpoints); Actor.roles carries the caller's
        full role list into StageTransitionService.transition(), which
        succeeds if ANY of them is HIRING_MANAGER (a dual-role actor is not
        locked out) and raises ForbiddenPipelineRoleException itself for a
        caller holding neither - not a route-level 403. Reason required
        (requires_reason=True); word-count-capped at the schema layer
        (RejectAtInterviewRequest), not here.
        """
        campaign_candidate = self.campaign_candidate_repo.get_by_id(campaign_candidate_id)
        if campaign_candidate is None:
            raise CampaignException("Campaign candidate not found.", 404)

        if "HR_ADMIN" not in actor_roles:
            self._assert_hiring_manager_owns_campaign(campaign_candidate, actor_id)

        if self.stage_transition_service is None:
            raise CampaignException("Stage transition service is not configured.", 500)

        self._transition_or_raise(
            campaign_candidate_id=campaign_candidate_id,
            to_stage=PipelineStage.REJECTED,
            actor=Actor(roles=actor_roles, id=actor_id),
            reason=decision_reason,
        )

        return self.get_campaign_candidate_scorecard(campaign_candidate_id)

    def send_rejection_email(
        self,
        campaign_candidate_id: UUID,
        actor_id: str,
        actor_roles: list[str],
    ) -> SendRejectionEmailResponse:
        """
        Manual "Send Rejection Email" action - the only way a rejection
        email is ever sent. No rejection path sends one on its own: neither
        the human ones (reject_at_interview, board drag-and-drop
        move_pipeline_stage, BulkStageMoveService) nor the automated
        deterministic/semantic/AI-evaluation rejections, which move the
        candidate to REJECTED but leave emailing to a recruiter.

        Reuses CandidateRejectionEmailService/the exact CANDIDATE_REJECTED
        template the automated path already uses - not a parallel
        implementation, and not a rejection-specific content path: the
        template only ever substitutes candidate_name/job_title, same as
        every other rejection email, so campaign_candidate.decision_reason
        is deliberately never surfaced here either.

        allow_resend=True: unlike feedback (a one-time, locked decision),
        re-sending a rejection notice (e.g. after fixing a template typo)
        is a reasonable, low-risk action - not over-engineering a dedup
        lock that doesn't serve a real purpose here.
        """
        campaign_candidate = self.campaign_candidate_repo.get_by_id(campaign_candidate_id)
        if campaign_candidate is None:
            raise CampaignException("Campaign candidate not found.", 404)

        if "HR_ADMIN" not in actor_roles:
            self._assert_hiring_manager_owns_campaign(campaign_candidate, actor_id)

        if campaign_candidate.pipeline_stage != PipelineStage.REJECTED:
            raise CampaignException("This candidate hasn't been rejected.", 400)

        db = self.campaign_candidate_repo.db
        email_service = CandidateRejectionEmailService(EmailTemplateRepository(db), EmailNotificationRepository(db))
        notification = email_service.queue_rejection_email(
            candidate_id=campaign_candidate.candidate_id,
            campaign_candidate_id=campaign_candidate.id,
            allow_resend=True,
        )
        if notification is None:
            # allow_resend=True already rules out the dedup branch - the
            # only way queue_rejection_email still returns None is no
            # active template configured, a real ops issue a human
            # clicking this button deserves to see, not a silent no-op.
            raise CampaignException("No active rejection email template is configured.", 500)

        send_candidate_email_task.apply_async(kwargs={"email_notification_id": str(notification.id)})
        return SendRejectionEmailResponse(status="queued")

    def send_selection_email(
        self,
        campaign_candidate_id: UUID,
        actor_id: str,
        actor_roles: list[str],
    ) -> SendSelectionEmailResponse:
        """
        Manual "Send Selection Email" action - mirrors send_rejection_email
        exactly. Reaching SELECTED (via select_candidate, board drag-and-
        drop/bulk move, or a stalled-candidate override) no longer queues
        this email automatically - a human explicitly triggers it once
        ready, same reasoning as the rejection flow: HR/HM may want to
        finalize offer details before the candidate is notified.

        queue_candidate_selected_email (unlike CandidateRejectionEmailService.
        queue_rejection_email) has no return value to inspect for "was
        there an active template" - it's shared with 0 remaining automatic
        callers now, but keeps its own best-effort/never-raise contract
        regardless. So the template check happens explicitly here first,
        the same outcome (a real ops issue surfaced to the human clicking
        the button, not a silent no-op) via a different mechanism.
        """
        campaign_candidate = self.campaign_candidate_repo.get_by_id(campaign_candidate_id)
        if campaign_candidate is None:
            raise CampaignException("Campaign candidate not found.", 404)

        if "HR_ADMIN" not in actor_roles:
            self._assert_hiring_manager_owns_campaign(campaign_candidate, actor_id)

        if campaign_candidate.pipeline_stage != PipelineStage.SELECTED:
            raise CampaignException("This candidate hasn't been selected.", 400)

        db = self.campaign_candidate_repo.db
        template = EmailTemplateRepository(db).get_active_by_trigger_event(EmailTriggerEvent.CANDIDATE_SELECTED)
        if template is None:
            raise CampaignException("No active selection email template is configured.", 500)

        queue_candidate_selected_email(db, campaign_candidate, allow_resend=True)
        return SendSelectionEmailResponse(status="queued")

    def bulk_send_rejection_email(
        self,
        campaign_candidate_ids: list[UUID],
        actor_id: str,
        actor_roles: list[str],
    ) -> BulkSendRejectionEmailResponse:
        """
        Bulk follow-up to send_rejection_email - calls that exact method per
        id rather than duplicating its validation (not found / ownership /
        must be REJECTED / no active template), so behavior for a single id
        inside a bulk call is always identical to calling the single-candidate
        endpoint directly.

        Deliberately NOT BulkStageMoveService.bulk_move's all-or-nothing
        shape: a stage move audits the batch as one event with a shared
        openings cap, so any one failure rolling back the whole thing is
        correct there. Sending an email per candidate has no such
        cross-candidate constraint, so each id's outcome is independent -
        one candidate's wrong stage or ownership failure never blocks the
        rest.
        """
        queued: list[UUID] = []
        failed: list[dict] = []
        for cc_id in campaign_candidate_ids:
            try:
                self.send_rejection_email(cc_id, actor_id=actor_id, actor_roles=actor_roles)
                queued.append(cc_id)
            except CampaignException as exc:
                failed.append({"campaign_candidate_id": str(cc_id), "reason": exc.message})

        return BulkSendRejectionEmailResponse(
            queued=queued,
            failed=failed,
            detail=f"Queued {len(queued)} rejection email(s), {len(failed)} failed.",
        )

    def _get_campaign_cached(
        self, campaign_id: UUID, campaign_cache: dict[UUID, HiringCampaign | None],
    ) -> HiringCampaign | None:
        if campaign_id not in campaign_cache:
            campaign_cache[campaign_id] = self.campaign_repo.get_by_id(campaign_id)
        return campaign_cache[campaign_id]

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
        breakdown = as_v2(rejection)
        # v2: a skills gap is a failed core-skill check (a found-but-unconfirmed skill too)
        skills_failed = skills_check_failed(breakdown)
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
        One entry per distinct canonical skill that didn't count toward the
        core gate (v2: missing, unconfirmed or voted against) across
        `rejections`, keyed by canonical_skill_id
        (falling back to canonical_name if the id is ever absent) so a
        platform-wide caller can join against JDSkill.canonical_skill_id.
        Sorted by occurrence count, descending.

        `rejections` are decision_details dicts (see _classify_rejection).
        """
        counts: dict = {}
        for rejection in rejections:
            for skill in unconfirmed_core_entries(rejection):
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
            return DEFAULT_MIN_CANDIDATES_FOR_ANALYTICS
        configs = self.config_repo.get_configs_by_keys([_MIN_CANDIDATES_FOR_ANALYTICS_KEY])
        raw = configs.get(_MIN_CANDIDATES_FOR_ANALYTICS_KEY)
        return int(raw) if raw is not None else DEFAULT_MIN_CANDIDATES_FOR_ANALYTICS

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
            # The current scoring version's experience tolerance (platform_config until settings phase 3).
            from app.services.campaign.validation_inputs import validation_settings
            raw = validation_settings(self.config_repo.db).get("experience_tolerance_years")
            current_tolerance = float(raw) if raw is not None else None

        return current_min_years, current_tolerance

    def _compute_rule3_override_rate(self, campaign_id: UUID) -> tuple[float, float]:
        """
        The campaign's override rate as the recruiter-overrides report's alert computes it
        (campaign_override_rate: candidates overridden against the system's outcome / candidates
        with a decided outcome, in %), with the same OVERRIDE_RATE_ALERT_THRESHOLD - so this
        recommendation and the dashboard alert always agree.
        """
        threshold = self._read_config_float(
            _OVERRIDE_RATE_ALERT_THRESHOLD_KEY, _DEFAULT_OVERRIDE_RATE_ALERT_THRESHOLD,
        )
        return campaign_override_rate(self.campaign_candidate_repo.db, campaign_id), threshold

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
        Reuses ExcelExport (no new XLSX engine) and AuditService. 2 sheets:
        Campaign Summary and Skill Gap Analysis. No candidate details.
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

        excel_file = ExcelExport.export_deterministic_rejection_summary(campaign_summary_rows, skill_gap_rows)
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
            },
        )

        # AuditRepository.create only flushes: commit, or the row is rolled back
        # when the request session closes (nothing else commits it).
        self.campaign_candidate_repo.commit()

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

        return {
            "campaign_name": campaign.name,
            "total_candidates": total_candidates,
            "deterministic_rejections": deterministic_rejections,
            "rejection_rate": rejection_rate,
            "top_rejection_reason": top_rejection_reason,
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

    def delete_campaign_candidate(
        self,
        campaign_candidate_id: UUID,
        actor_id: str,
        actor_role: str | None = None,
        actor_roles: list[str] | None = None,
    ) -> None:
        """
        Delete a campaign candidate - scoped to this one campaign
        application only (see test_campaign_candidate_delete.py for the
        guarantee that this never reaches the shared Candidate/Resume rows
        or any other campaign that candidate is also in - contrast with
        the deliberately global DELETE /candidates/{id} GDPR erasure).

        Bug fix: every one of these tables carries an FK to
        campaign_candidates.id with no ON DELETE rule, so a bare
        campaign_candidate_repo.delete() raised a raw
        psycopg2.errors.ForeignKeyViolation - not the 404/409 this method's
        callers expect - for any candidate that wasn't still sitting
        untouched at UPLOADED (i.e. had ever recorded a stage, a note, an
        interview round, or a processing task). Same set of tables
        CandidateErasureService.erase_candidate already clears per
        candidate, here scoped to campaign_candidate_id alone so a
        candidate present in another campaign is never touched. Order
        matters only where noted (DLQ before celery_task_log;
        campaign_candidate_ai_evaluations needs no explicit call - it
        cascades via cascade="all, delete-orphan" on
        CampaignCandidate.ai_evaluation when the parent row is deleted
        below).
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

            # Only HR_ADMIN, or a recruiter whose campaign it is (assigned
            # recruiter or creator), may remove a candidate from it.
            # actor_roles=None: an internal caller.
            if actor_roles is not None and "HR_ADMIN" not in actor_roles:
                from app.repositories.recruiter_campaigns import is_recruiters_campaign
                campaign = self.campaign_repo.get_by_id(candidate.campaign_id)
                if not ("RECRUITER" in actor_roles and is_recruiters_campaign(campaign, actor_id)):
                    raise CampaignException(
                        "Only an HR admin or this campaign's recruiter can remove a candidate from it.",
                        403,
                    )

            # Captured before delete/commit: the session's default
            # expire_on_commit=True means `candidate`'s attributes would
            # otherwise trigger a re-SELECT of a now-deleted row on next
            # access (raising ObjectDeletedError) - both the audit log call
            # and the WebSocket publish below need these as plain values.
            deleted_id = candidate.id
            deleted_campaign_id = candidate.campaign_id
            deleted_candidate_id = candidate.candidate_id
            deleted_resume_id = candidate.resume_id

            self.email_notification_repo.delete_by_campaign_candidate_id(deleted_id)
            self.campaign_candidate_repo.delete_stage_history(deleted_id)
            self.campaign_candidate_repo.delete_stage_transition_log(deleted_id)
            self.candidate_note_repo.delete_by_campaign_candidate_id(deleted_id)
            # Also clears interview_interviewers/interview_schedule_history/
            # interview_feedback/interview-round email_notifications - see
            # its own docstring.
            self.interview_schedule_repo.delete_by_campaign_candidate_id(deleted_id)
            self.composite_score_history_repo.delete_by_campaign_candidate_id(deleted_id)
            # v1 / v2 scoring results and a shadow-report recruiter rank.
            self.campaign_candidate_repo.delete_scoring_results(deleted_id)
            self.campaign_candidate_repo.delete_shadow_ranking(deleted_id)
            # DLQ before celery_task_log - dead_letter_queue.original_task_id
            # is a NOT NULL FK to celery_task_log.task_id.
            self.dead_letter_queue_repo.delete_by_campaign_candidate_id(deleted_id)
            self.celery_task_log_repo.delete_by_campaign_candidate_id(deleted_id)
            # Pending "keep existing / use new version" decisions for this
            # candidate here: kept existing (audited), held files deleted
            # after the commit. The rows stay as history.
            held_files = []
            if self.upload_decision_service is not None:
                held_files = self.upload_decision_service.close_pending_for_removed_campaign_candidate(
                    deleted_id, actor_id=actor_id, actor_role=actor_role,
                )

            self.campaign_candidate_repo.delete(candidate)

            # Audit Log - in the same transaction as the delete, so the removal
            # and its record are committed together (AuditRepository.create
            # only flushes; logging after the commit left it uncommitted).
            self.audit_service.log(
                actor_id=actor_id,
                actor_role=actor_role,
                action_type=ActionType.CANDIDATE_REMOVED,
                entity_type=EntityType.CAMPAIGN_CANDIDATE,
                entity_id=deleted_id,
                campaign_id=deleted_campaign_id,
                details={
                    "candidate_id": str(deleted_candidate_id),
                    "resume_id": str(deleted_resume_id),
                },
            )

            self.campaign_candidate_repo.commit()

            if held_files:
                self.upload_decision_service.delete_held_files(held_files)

            CandidateChangeNotifier().removed(deleted_campaign_id, deleted_id, [deleted_resume_id])

        except Exception:
            self.campaign_candidate_repo.rollback()
            raise
