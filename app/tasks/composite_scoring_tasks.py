import json
import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.core.celery_app import celery_app
from app.db.session import SessionLocal
from app.models.async_tasks import FailureClassification, TaskStatus
from app.models.campaigns import CampaignStatus
from app.models.pipeline import CompositeScoreTriggerSource
from app.repositories.audit_repository import AuditRepository
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.candidate_composite_score_history_repository import (
    CandidateCompositeScoreHistoryRepository,
)
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.dead_letter_queue_repository import DeadLetterQueueRepository
from app.services.audit_service import AuditService
from app.services.campaign.composite_scoring_service import CompositeScoringService
from app.services.celery_task_log_service import CeleryTaskLogService
from app.services.document_processing.error_classifier import classify
from app.services.document_processing.retry_policy import RetryPolicy, compute_backoff_seconds

logger = logging.getLogger(__name__)

# Must match _enqueue_composite_scoring's own idempotency lookups exactly -
# duplicated (not imported) across service/task boundaries, same convention
# already established for SEMANTIC_SCORE_TASK_TYPE/AI_EVALUATE_TASK_TYPE.
COMPOSITE_SCORE_TASK_TYPE = "COMPOSITE_SCORE"

# Same campaign-status gate every other scoring task uses - a CLOSED
# campaign is a legitimate reason to skip, not a failure.
_SCOREABLE_CAMPAIGN_STATUSES = {CampaignStatus.ACTIVE, CampaignStatus.PAUSED}

# Composite scoring never depends on another async pipeline finishing (its
# inputs - deterministic_score/semantic_score/effective_ai_score - are read
# as of whenever this task actually runs), but a transient DB hiccup is
# still worth a bounded retry rather than an immediate dead-letter, same
# policy shape as semantic scoring's.
_COMPOSITE_SCORE_RETRY_POLICY = RetryPolicy(max_attempts=3, base_delay_seconds=10, max_delay_seconds=120)

def _enqueue_composite_scoring(
    campaign_candidate_id: UUID,
    task_log_service: CeleryTaskLogService,
    trigger_source: CompositeScoreTriggerSource,
) -> None:
    """
    Shared composite-scoring enqueue helper - the single place this is
    done, reused by every valid trigger site: AI evaluation completing with
    a non-REJECT recommendation, a rejection at any of the 3 automated
    screening layers (deterministic_scoring_tasks.py/semantic_scoring_tasks.py/
    ai_evaluation_tasks.py, each passing CompositeScoreTriggerSource.REJECTION),
    and campaign weight changes
    (CampaignService._enqueue_composite_recalculation_for_campaign). An HR
    override is deliberately NOT a trigger site - it only restarts the
    remaining scoring pipeline (deterministic re-pass -> semantic -> AI
    evaluation); it is that pipeline's own eventual success/rejection which
    (re)triggers this. Idempotency: a QUEUED/RUNNING celery_task_log row for
    this campaign_candidate_id + COMPOSITE_SCORE already means a
    calculation is in flight - never a second/parallel idempotency
    mechanism. No locking (Design Decision 8): if two triggers race, both
    may enqueue and run; whichever commits last simply overwrites
    composite_score with its own freshly-read values, which is acceptable
    since composite_score is entirely derived data.
    """
    task_log_repo = task_log_service.repository
    already_queued = any(
        log.status in (TaskStatus.QUEUED, TaskStatus.RUNNING)
        for log in task_log_repo.get_by_campaign_candidate_and_task_type(
            campaign_candidate_id, COMPOSITE_SCORE_TASK_TYPE,
        )
    )
    if already_queued:
        return

    log = task_log_service.create_log(
        task_id=str(uuid4()),
        task_type=COMPOSITE_SCORE_TASK_TYPE,
        campaign_candidate_id=campaign_candidate_id,
    )
    try:
        calculate_composite_score_task.apply_async(
            kwargs={
                "campaign_candidate_id": str(campaign_candidate_id),
                "trigger_source": trigger_source.value,
            },
            task_id=log.task_id,
        )
    except Exception:
        logger.exception(
            "Failed to enqueue COMPOSITE_SCORE for campaign_candidate_id=%s", campaign_candidate_id,
        )


@celery_app.task(name="scoring.calculate_composite_score", bind=True)
def calculate_composite_score_task(self, campaign_candidate_id: str, trigger_source: str) -> None:
    """
    M10-E01: Composite Score calculation for one campaign_candidate.
    Combines deterministic_score/semantic_score/effective_ai_score
    (COALESCEd to 0 wherever missing - see CompositeScoringService),
    weighted by the owning campaign's scoring weights exactly as
    configured (no redistribution), never recomputing any of those three
    inputs itself. Only ever enqueued for one of 3 reasons: AI evaluation
    completing with a non-REJECT recommendation, a rejection at any
    automated screening layer, or a campaign's scoring weights changing -
    never on resume upload/parsing/reprocessing/reset or an HR override
    (an override only restarts the remaining scoring pipeline; it is that
    pipeline's own eventual success/rejection which (re)triggers this).

    Mirrors calculate_semantic_score_task's overall shape: existence
    checks, campaign status gate, retry/dead-letter machinery (RetryPolicy +
    error_classifier + DeadLetterQueue) reused as-is rather than
    reimplemented. Unlike deterministic/semantic scoring, this task never
    creates a candidate_rejection or transitions pipeline_stage - composite
    scoring is a ranking signal, not a pass/fail gate.
    """
    db = SessionLocal()
    task_log = None
    task_id = self.request.id
    attempt_number = self.request.retries + 1
    try:
        campaign_candidate_repo = CampaignCandidateRepository(db)
        campaign_repo = CampaignRepository(db)
        history_repo = CandidateCompositeScoreHistoryRepository(db)
        audit_service = AuditService(AuditRepository(db))
        task_log_repo = CeleryTaskLogRepository(db)
        task_log_service = CeleryTaskLogService(task_log_repo)

        campaign_candidate = campaign_candidate_repo.get_by_id(UUID(campaign_candidate_id))

        existing_task_log = task_log_repo.get_by_task_id(task_id)
        # Same broker-redelivery guard as every other scoring task: only a
        # completed (SUCCESS) run short-circuits.
        if existing_task_log is not None and existing_task_log.status == TaskStatus.SUCCESS:
            logger.info(
                "Composite scoring already completed for task_id=%s campaign_candidate_id=%s - skipping.",
                task_id, campaign_candidate_id,
            )
            return

        if existing_task_log is None:
            existing_task_log = task_log_service.create_log(
                task_id=task_id,
                task_type=COMPOSITE_SCORE_TASK_TYPE,
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
                "Composite scoring skipped | campaign_candidate_id=%s reason=campaign_candidate_deleted",
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
                "Composite scoring skipped | campaign_candidate_id=%s reason=campaign_status_%s",
                campaign_candidate_id, campaign.status.value,
            )
            return

        scoring_service = CompositeScoringService(campaign_candidate_repo, campaign_repo, history_repo, audit_service)
        breakdown = scoring_service.calculate_and_store_composite_score(
            campaign_candidate.id, CompositeScoreTriggerSource(trigger_source),
        )

        summary_payload = {
            "composite_score": breakdown["composite_score"],
            "formula_version": breakdown["formula_version"],
            "trigger_source": breakdown["trigger_source"],
            "composite_score_breakdown": breakdown,
        }

        campaign_candidate_repo.commit()

        task_log_service.mark_success(task_log, summary=json.dumps(summary_payload))

    except Exception as ex:
        db.rollback()
        classification = classify(ex)

        if classification != FailureClassification.PERMANENT and attempt_number < _COMPOSITE_SCORE_RETRY_POLICY.max_attempts:
            if task_log:
                task_log_service.mark_retry(task_log)
            delay = compute_backoff_seconds(_COMPOSITE_SCORE_RETRY_POLICY, attempt_number)
            logger.warning(
                "Composite scoring transient failure, retrying | campaign_candidate_id=%s attempt=%s delay=%ss error=%s",
                campaign_candidate_id, attempt_number, delay, ex,
            )
            self.retry(exc=ex, countdown=delay, max_retries=_COMPOSITE_SCORE_RETRY_POLICY.max_attempts)
            return

        # Retries exhausted (or a permanent failure, e.g. invalid scoring
        # weights) - dead-letter, mark the task_log DEAD, log the failure
        # reason. Never re-raised - this is now dead-lettered/terminal
        # bookkeeping, same convention as calculate_semantic_score_task.
        error_message = str(ex)
        try:
            DeadLetterQueueRepository(db).create(
                original_task_id=task_id,
                task_type=COMPOSITE_SCORE_TASK_TYPE,
                final_error_message=error_message,
                full_error_trace=None,
                input_payload={"campaign_candidate_id": campaign_candidate_id, "trigger_source": trigger_source},
                retry_count=attempt_number,
                first_attempted_at=task_log.queued_at if task_log else datetime.now(timezone.utc),
                last_attempted_at=datetime.now(timezone.utc),
                campaign_candidate_id=UUID(campaign_candidate_id),
            )
            DeadLetterQueueRepository(db).commit()
        except Exception:
            logger.exception(
                "Failed to dead-letter composite scoring for campaign_candidate_id=%s", campaign_candidate_id,
            )
            db.rollback()

        if task_log:
            task_log_service.mark_dead(task_log, error_message)
        logger.exception("Composite scoring task permanently failed for campaign_candidate_id %s", campaign_candidate_id)

    finally:
        db.close()
