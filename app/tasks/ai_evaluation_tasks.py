import json
import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.core.celery_app import celery_app
from app.db.session import SessionLocal
from app.enums.constants import ActionType, EntityType
from app.models.async_tasks import FailureClassification, TaskStatus
from app.models.campaigns import CampaignStatus
from app.models.pipeline import CompositeScoreTriggerSource, DecisionSource, DecisionType, PipelineStage
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.allowed_transition_repository import AllowedTransitionRepository
from app.repositories.audit_repository import AuditRepository
from app.repositories.campaign_candidate_ai_evaluation_repository import CampaignCandidateAIEvaluationRepository
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.dead_letter_queue_repository import DeadLetterQueueRepository
from app.repositories.interview_schedule_repository import InterviewScheduleRepository
from app.repositories.jd_repository import JDRepository
from app.repositories.prompt_template_repository import PromptTemplateRepository
from app.repositories.resume_repository import ResumeRepository
from app.services.audit_service import AuditService
from app.services.campaign.ai_evaluation_service import AIEvaluationService
from app.services.campaign.stage_transition_service import StageTransitionService
from app.services.celery_task_log_service import CeleryTaskLogService
from app.services.document_processing.error_classifier import classify
from app.services.document_processing.retry_policy import RetryPolicy, compute_backoff_seconds
from app.services.extractions.gemini_extraction_service import GeminiExtractionService
from app.services.prompt_template_validation import validate_prompt_template_selection
from app.tasks.composite_scoring_tasks import _enqueue_composite_scoring
from app.tasks.deterministic_scoring_tasks import _queue_rejection_email
from app.websocket.publisher import publish_board_candidate_updated

logger = logging.getLogger(__name__)

# Must match deterministic_scoring_tasks.AI_EVALUATE_TASK_TYPE and
# campaign_candidate_service.AI_EVALUATE_TASK_TYPE exactly - duplicated
# (not imported) to avoid a circular import between the task and service
# layers, same convention those two modules already established for this
# same string.
AI_EVALUATE_TASK_TYPE = "AI_EVALUATE"

# Same campaign-status gate as calculate_deterministic_score_task/
# calculate_semantic_score_task - a CLOSED campaign is a legitimate reason
# to skip, not a failure.
_SCOREABLE_CAMPAIGN_STATUSES = {CampaignStatus.ACTIVE, CampaignStatus.PAUSED}

# Same shape as semantic_scoring_tasks._SEMANTIC_SCORE_RETRY_POLICY - an
# LLM call is exactly the transient-failure-prone case that policy exists
# for (Gemini timeout/rate-limit/network failure), reused as-is rather than
# inventing a third retry shape for screening.
_AI_EVALUATION_RETRY_POLICY = RetryPolicy(max_attempts=3, base_delay_seconds=10, max_delay_seconds=120)


def _enqueue_ai_evaluation(
    campaign_candidate, task_log_service: CeleryTaskLogService,
) -> None:
    """
    Chains directly off calculate_semantic_score_task's own success path,
    exactly the way _enqueue_semantic_scoring (semantic_scoring_tasks.py)
    chains off calculate_deterministic_score_task's - only ever called
    after breakdown["semantic_passed"] is True and that task's own
    transaction has already committed. This is the terminal screening
    stage - nothing is ever enqueued from here.

    Idempotency: identical convention to _enqueue_semantic_scoring - a
    QUEUED/RUNNING celery_task_log row for this campaign_candidate_id +
    AI_EVALUATE already means evaluation is in flight; never a
    second/parallel idempotency mechanism.
    """
    task_log_repo = task_log_service.repository
    already_queued = any(
        log.status in (TaskStatus.QUEUED, TaskStatus.RUNNING)
        for log in task_log_repo.get_by_campaign_candidate_and_task_type(
            campaign_candidate.id, AI_EVALUATE_TASK_TYPE,
        )
    )
    if already_queued:
        return

    log = task_log_service.create_log(
        task_id=str(uuid4()),
        task_type=AI_EVALUATE_TASK_TYPE,
        campaign_candidate_id=campaign_candidate.id,
    )
    try:
        calculate_ai_evaluation_task.apply_async(
            kwargs={"campaign_candidate_id": str(campaign_candidate.id)},
            task_id=log.task_id,
        )
    except Exception:
        logger.exception(
            "Failed to enqueue AI_EVALUATE for campaign_candidate_id=%s", campaign_candidate.id,
        )


@celery_app.task(name="scoring.calculate_ai_evaluation", bind=True)
def calculate_ai_evaluation_task(self, campaign_candidate_id: str) -> None:
    """
    Terminal screening stage: independently evaluates a candidate against
    the job using ONLY the already-extracted Resume JSON (resume.
    parsed_json) and Job Description JSON (job_description.extracted_json)
    - never raw text, never the deterministic/semantic scores or
    explanations those independent layers already computed. Enqueued
    exclusively from calculate_semantic_score_task's success path (see
    _enqueue_ai_evaluation), after a candidate has passed semantic
    screening. Mirrors calculate_semantic_score_task's overall shape
    (idempotency, campaign-status gate, RetryPolicy + error_classifier +
    DeadLetterQueue retry/dead-letter handling, task-owns-the-transaction
    persistence pattern) exactly.

    Phase 2.4: persists the related CampaignCandidateAIEvaluation row (via
    AIEvaluationService.calculate_and_store_evaluation), sets the unified
    decision fields (decision_source=AI) and transitions pipeline_stage via
    StageTransitionService on a REJECT recommendation, and writes an
    AI_EVALUATION_COMPUTED audit log entry - same division of
    responsibility as calculate_deterministic_score_task/
    calculate_semantic_score_task: the service mutates+flushes, this task
    owns the rejection/stage-transition/audit decisions and the single
    commit. Enqueues nothing further - this is still the terminal
    screening stage.
    """
    db = SessionLocal()
    task_log = None
    task_id = self.request.id
    attempt_number = self.request.retries + 1
    try:
        campaign_candidate_repo = CampaignCandidateRepository(db)
        campaign_repo = CampaignRepository(db)
        resume_repo = ResumeRepository(db)
        jd_repo = JDRepository(db)
        prompt_template_repo = PromptTemplateRepository(db)
        ai_evaluation_repo = CampaignCandidateAIEvaluationRepository(db)
        allowed_transition_repo = AllowedTransitionRepository(db)
        audit_service = AuditService(AuditRepository(db))
        task_log_repo = CeleryTaskLogRepository(db)
        task_log_service = CeleryTaskLogService(task_log_repo)
        interview_schedule_repo = InterviewScheduleRepository(db)
        stage_transition_service = StageTransitionService(
            allowed_transition_repo, campaign_candidate_repo, audit_service, interview_schedule_repo,
        )

        campaign_candidate = campaign_candidate_repo.get_by_id(UUID(campaign_candidate_id))

        existing_task_log = task_log_repo.get_by_task_id(task_id)
        # Same broker-redelivery guard as calculate_deterministic_score_task/
        # calculate_semantic_score_task: only a completed (SUCCESS) run
        # short-circuits - RUNNING/FAILURE/RETRY are still reprocessed.
        if existing_task_log is not None and existing_task_log.status == TaskStatus.SUCCESS:
            logger.info(
                "AI evaluation already completed for task_id=%s campaign_candidate_id=%s - skipping.",
                task_id, campaign_candidate_id,
            )
            return

        if existing_task_log is None:
            existing_task_log = task_log_service.create_log(
                task_id=task_id,
                task_type=AI_EVALUATE_TASK_TYPE,
                campaign_candidate_id=campaign_candidate.id if campaign_candidate is not None else None,
            )
        task_log = task_log_service.mark_running(existing_task_log)

        if campaign_candidate is None:
            summary = json.dumps({
                "skipped": True,
                "reason": f"campaign_candidate_id {campaign_candidate_id} no longer exists.",
            })
            task_log_service.mark_success(task_log, summary=summary)
            logger.warning(
                "AI evaluation skipped | campaign_candidate_id=%s reason=campaign_candidate_deleted",
                campaign_candidate_id,
            )
            return

        campaign = campaign_repo.get_by_id(campaign_candidate.campaign_id)
        if campaign is None:
            raise ValueError(f"Campaign '{campaign_candidate.campaign_id}' not found.")

        if campaign.status not in _SCOREABLE_CAMPAIGN_STATUSES:
            summary = json.dumps({"skipped": True, "reason": f"Campaign status is {campaign.status.value}."})
            task_log_service.mark_success(task_log, summary=summary)
            logger.info(
                "AI evaluation skipped | campaign_candidate_id=%s reason=campaign_status_%s",
                campaign_candidate_id, campaign.status.value,
            )
            return

        # A candidate rejected at an earlier layer (DETERMINISTIC or
        # SEMANTIC) must never be evaluated here. _cancel_downstream_ai_
        # evaluation (deterministic_scoring_tasks.py) already dead-letters
        # a still-QUEUED AI_EVALUATE log on rejection - this is the in-task
        # defensive check for a task that was already RUNNING/redelivered
        # past that point.
        if campaign_candidate.pipeline_stage == PipelineStage.REJECTED:
            summary = json.dumps({
                "skipped": True,
                "reason": "Candidate was rejected at an earlier screening layer - AI evaluation does not apply.",
            })
            task_log_service.mark_success(task_log, summary=summary)
            logger.info(
                "AI evaluation skipped | campaign_candidate_id=%s reason=candidate_rejected",
                campaign_candidate_id,
            )
            return

        semantic_breakdown = campaign_candidate.semantic_breakdown or {}
        if not semantic_breakdown.get("semantic_passed"):
            summary = json.dumps({
                "skipped": True,
                "reason": "Candidate has not passed semantic screening - AI evaluation does not apply.",
            })
            task_log_service.mark_success(task_log, summary=summary)
            logger.info(
                "AI evaluation skipped | campaign_candidate_id=%s reason=semantic_not_passed",
                campaign_candidate_id,
            )
            return

        resume = resume_repo.get_by_id(campaign_candidate.resume_id)
        if resume is None or not resume.parsed_json:
            raise ValueError(
                f"Resume '{campaign_candidate.resume_id}' has no parsed_json - AI evaluation cannot run."
            )

        job_description = jd_repo.get_by_id(campaign.jd_id)
        if job_description is None or not job_description.extracted_json:
            raise ValueError(
                f"Job description '{campaign.jd_id}' has no extracted_json - AI evaluation cannot run."
            )

        prompt = validate_prompt_template_selection(
            campaign.ai_evaluate_prompt_id,
            expected_task_type=AI_EVALUATE_TASK_TYPE,
            repository=prompt_template_repo,
            exception_factory=ValueError,
        )

        evaluation_service = AIEvaluationService(
            GeminiExtractionService(), campaign_candidate_repo, ai_evaluation_repo,
        )
        ai_response = evaluation_service.calculate_and_store_evaluation(
            campaign_candidate_id=campaign_candidate.id,
            resume_json=resume.parsed_json,
            jd_json=job_description.extracted_json,
            prompt_template_text=prompt.template_text,
        )

        recommendation = ai_response["recommendation"]
        stage_transition_succeeded = False
        if recommendation == "REJECT":
            rejection_reason = AIEvaluationService.build_rejection_reason(ai_response)

            stage_transition_succeeded = stage_transition_service.transition_to_rejected(
                campaign_candidate,
                change_reason="AI evaluation rejection",
                scores_snapshot=ai_response,
                decision_source=DecisionSource.AI,
                decision_reason=rejection_reason,
                decision_details=ai_response,
            )
        elif recommendation == "SHORTLIST":
            stage_transition_succeeded = stage_transition_service.transition_on_ai_success(
                campaign_candidate,
                to_stage=PipelineStage.SHORTLISTED,
                decision_type=DecisionType.SHORTLISTED,
                change_reason="AI evaluation shortlist recommendation",
                scores_snapshot=ai_response,
                decision_reason="AI evaluation recommended shortlisting this candidate.",
                decision_details=ai_response,
            )
        elif recommendation == "HOLD":
            stage_transition_succeeded = stage_transition_service.transition_on_ai_success(
                campaign_candidate,
                to_stage=PipelineStage.HOLD,
                decision_type=DecisionType.HOLD,
                change_reason="AI evaluation hold recommendation",
                scores_snapshot=ai_response,
                decision_reason="AI evaluation recommended placing this candidate on hold.",
                decision_details=ai_response,
            )

        summary_payload = {
            "campaign_candidate_id": str(campaign_candidate.id),
            "campaign_id": str(campaign.id),
            "resume_id": str(campaign_candidate.resume_id),
            "recommendation": recommendation,
            "overall_score": ai_response["scores"]["overall_score"],
            "confidence_score": ai_response["confidence_score"],
            "ai_response": ai_response,
        }

        audit_service.log(
            actor_id=None,
            actor_role="SYSTEM",
            action_type=ActionType.AI_EVALUATION_COMPUTED,
            entity_type=EntityType.CAMPAIGN_CANDIDATE,
            entity_id=campaign_candidate.id,
            campaign_id=campaign.id,
            details=summary_payload,
        )

        campaign_candidate_repo.commit()

        try:
            publish_board_candidate_updated(campaign.id, campaign_candidate.id)
        except Exception:
            logger.exception(
                "Failed to publish board.candidate_updated for campaign_candidate_id=%s",
                campaign_candidate.id,
            )

        task_log_service.mark_success(task_log, summary=json.dumps(summary_payload))

        logger.info(
            "AI evaluation completed | campaign_candidate_id=%s recommendation=%s",
            campaign_candidate.id, recommendation,
        )

        if recommendation == "REJECT" and stage_transition_succeeded:
            _queue_rejection_email(db, campaign_candidate)

        # Composite score's 2 automated-pipeline triggers: AI evaluation
        # completing with a non-REJECT recommendation (this is the terminal
        # "success" outcome), or a REJECT recommendation that actually
        # transitioned pipeline_stage to REJECTED (mirrors the deterministic/
        # semantic layers' own REJECTION trigger on their own rejections).
        try:
            if recommendation == "REJECT":
                if stage_transition_succeeded:
                    _enqueue_composite_scoring(
                        campaign_candidate.id, task_log_service, CompositeScoreTriggerSource.REJECTION,
                    )
            else:
                _enqueue_composite_scoring(
                    campaign_candidate.id, task_log_service, CompositeScoreTriggerSource.AI_EVALUATION,
                )
        except Exception:
            logger.exception(
                "Failed to enqueue composite scoring after AI evaluation for campaign_candidate_id=%s",
                campaign_candidate.id,
            )

    except Exception as ex:
        db.rollback()
        classification = classify(ex)

        if classification != FailureClassification.PERMANENT and attempt_number < _AI_EVALUATION_RETRY_POLICY.max_attempts:
            if task_log:
                task_log_service.mark_retry(task_log)
            delay = compute_backoff_seconds(_AI_EVALUATION_RETRY_POLICY, attempt_number)
            logger.warning(
                "AI evaluation transient failure, retrying | campaign_candidate_id=%s attempt=%s delay=%ss error=%s",
                campaign_candidate_id, attempt_number, delay, ex,
            )
            self.retry(exc=ex, countdown=delay, max_retries=_AI_EVALUATION_RETRY_POLICY.max_attempts)
            return

        # Retries exhausted (or a permanent failure) - dead-letter, mark the
        # task_log DEAD, log the failure reason. Never re-raised: this is
        # now dead-lettered/terminal bookkeeping, same convention as
        # calculate_semantic_score_task.
        error_message = str(ex)
        try:
            DeadLetterQueueRepository(db).create(
                original_task_id=task_id,
                task_type=AI_EVALUATE_TASK_TYPE,
                final_error_message=error_message,
                full_error_trace=None,
                input_payload={"campaign_candidate_id": campaign_candidate_id},
                retry_count=attempt_number,
                first_attempted_at=task_log.queued_at if task_log else datetime.now(timezone.utc),
                last_attempted_at=datetime.now(timezone.utc),
                campaign_candidate_id=UUID(campaign_candidate_id),
            )
            DeadLetterQueueRepository(db).commit()
        except Exception:
            logger.exception(
                "Failed to dead-letter AI evaluation for campaign_candidate_id=%s", campaign_candidate_id,
            )
            db.rollback()

        if task_log:
            task_log_service.mark_dead(task_log, error_message)
        logger.exception(
            "AI evaluation task permanently failed for campaign_candidate_id %s", campaign_candidate_id,
        )

    finally:
        db.close()
