import json
import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.core.celery_app import celery_app
from app.db.session import SessionLocal
from app.enums.constants import ActionType, EntityType
from app.models.async_tasks import FailureClassification, TaskStatus
from app.models.campaigns import CampaignStatus
from app.models.pipeline import CandidateRejection, RejectionLayer
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.allowed_transition_repository import AllowedTransitionRepository
from app.repositories.audit_repository import AuditRepository
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.candidate_rejection_repository import CandidateRejectionRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.dead_letter_queue_repository import DeadLetterQueueRepository
from app.repositories.jd_repository import JDRepository
from app.repositories.resume_repository import ResumeRepository
from app.services.audit_service import AuditService
from app.services.campaign.semantic_scoring_service import SemanticScoringService
from app.services.campaign.stage_transition_service import StageTransitionService
from app.services.celery_task_log_service import CeleryTaskLogService
from app.services.document_processing.error_classifier import classify
from app.services.document_processing.retry_policy import RetryPolicy, compute_backoff_seconds
from app.tasks.deterministic_scoring_tasks import _cancel_downstream_ai_evaluation

logger = logging.getLogger(__name__)

# Must match campaign_candidate_service.SEMANTIC_SCORE_TASK_TYPE exactly -
# duplicated (not imported) to avoid a circular import between the service
# and task layers, same convention already established for
# AI_EVALUATE_TASK_TYPE (see deterministic_scoring_tasks.py /
# campaign_candidate_service.py, both of which independently define the
# same "AI_EVALUATE" string with a cross-referencing comment).
SEMANTIC_SCORE_TASK_TYPE = "SEMANTIC_SCORE"

# Same campaign-status gate as calculate_deterministic_score_task - a
# CLOSED campaign is a legitimate reason to skip, not a failure.
_SCOREABLE_CAMPAIGN_STATUSES = {CampaignStatus.ACTIVE, CampaignStatus.PAUSED}

# M08-E02: same shape as embedding_tasks.py's _EMBED_RESUME_RETRY_POLICY -
# this task (unlike deterministic scoring) depends on another async
# pipeline's output (resume/JD embeddings) that may not exist yet at run
# time, so it needs real retry/dead-letter handling, not deterministic
# scoring's immediate-fail-and-raise.
_SEMANTIC_SCORE_RETRY_POLICY = RetryPolicy(max_attempts=3, base_delay_seconds=10, max_delay_seconds=120)


def _enqueue_semantic_scoring(
    campaign_candidate, task_log_service: CeleryTaskLogService, resume_repo: ResumeRepository,
) -> None:
    """
    Shared semantic-scoring enqueue helper - the single place this is done,
    reused by both call sites that need it:
      - calculate_deterministic_score_task, immediately after a candidate
        passes deterministic screening and that task's own transaction has
        already committed (M08-E02 auto-trigger).
      - CampaignCandidateService._queue_post_override_evaluation, after an
        HR_ADMIN override of a deterministic rejection.

    Idempotency: a QUEUED/RUNNING celery_task_log row for this
    campaign_candidate_id + SEMANTIC_SCORE already means scoring is in
    flight - never a second/parallel idempotency mechanism, and never
    duplicated between the two call sites. Silently no-ops if the resume
    has no embedding yet (EMBED_RESUME may still be running) - neither
    caller is ever blocked or made to wait on it.
    """
    if resume_repo.get_embedding(campaign_candidate.resume_id) is None:
        return

    task_log_repo = task_log_service.repository
    already_queued = any(
        log.status in (TaskStatus.QUEUED, TaskStatus.RUNNING)
        for log in task_log_repo.get_by_campaign_candidate_and_task_type(
            campaign_candidate.id, SEMANTIC_SCORE_TASK_TYPE,
        )
    )
    if already_queued:
        return

    log = task_log_service.create_log(
        task_id=str(uuid4()),
        task_type=SEMANTIC_SCORE_TASK_TYPE,
        campaign_candidate_id=campaign_candidate.id,
    )
    try:
        calculate_semantic_score_task.apply_async(
            kwargs={"campaign_candidate_id": str(campaign_candidate.id)},
            task_id=log.task_id,
        )
    except Exception:
        logger.exception(
            "Failed to enqueue SEMANTIC_SCORE for campaign_candidate_id=%s", campaign_candidate.id,
        )


@celery_app.task(name="scoring.calculate_semantic_score", bind=True)
def calculate_semantic_score_task(self, campaign_candidate_id: str) -> None:
    """
    M08-E02 Phase 2: Semantic Similarity Scoring for one campaign_candidate.
    Reuses already-generated resume/JD embeddings (M08-E01's EMBED_RESUME
    and the JD processing pipeline) - never regenerates either. Mirrors
    calculate_deterministic_score_task's overall shape (existence checks,
    campaign status gate, rejection + stage transition + downstream
    AI-evaluation cancellation on failure, audit logging via the same
    CAMPAIGN_CANDIDATE entity type), plus embedding_tasks.py's retry/
    dead-letter machinery (RetryPolicy + error_classifier + DeadLetterQueue),
    reused as-is rather than reimplemented.

    Only runs for a candidate that already passed deterministic screening
    (deterministic_passed=True) - a candidate rejected at the deterministic
    layer, or not yet screened at all, is skipped gracefully, never scored.
    Only semantic_score/semantic_score_breakdown are ever written here -
    deterministic_score/score_breakdown/screened_at are never touched.
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
        config_repo = ConfigRepository(db)
        candidate_rejection_repo = CandidateRejectionRepository(db)
        allowed_transition_repo = AllowedTransitionRepository(db)
        audit_service = AuditService(AuditRepository(db))
        task_log_repo = CeleryTaskLogRepository(db)
        task_log_service = CeleryTaskLogService(task_log_repo)
        stage_transition_service = StageTransitionService(allowed_transition_repo, campaign_candidate_repo)

        campaign_candidate = campaign_candidate_repo.get_by_id(UUID(campaign_candidate_id))

        existing_task_log = task_log_repo.get_by_task_id(task_id)
        # Same broker-redelivery guard as calculate_deterministic_score_task/
        # generate_resume_embedding_task: only a completed (SUCCESS) run
        # short-circuits - RUNNING/FAILURE/RETRY are still reprocessed.
        if existing_task_log is not None and existing_task_log.status == TaskStatus.SUCCESS:
            logger.info(
                "Semantic scoring already completed for task_id=%s campaign_candidate_id=%s - skipping.",
                task_id, campaign_candidate_id,
            )
            return

        if existing_task_log is None:
            existing_task_log = task_log_service.create_log(
                task_id=task_id,
                task_type=SEMANTIC_SCORE_TASK_TYPE,
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
                "Semantic scoring skipped | campaign_candidate_id=%s reason=campaign_candidate_deleted",
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
                "Semantic scoring skipped | campaign_candidate_id=%s reason=campaign_status_%s",
                campaign_candidate_id, campaign.status.value,
            )
            return

        if not campaign_candidate.deterministic_passed:
            summary = json.dumps({
                "skipped": True,
                "reason": "Candidate has not passed deterministic screening - semantic scoring does not apply.",
            })
            task_log_service.mark_success(task_log, summary=summary)
            logger.info(
                "Semantic scoring skipped | campaign_candidate_id=%s reason=deterministic_not_passed",
                campaign_candidate_id,
            )
            return

        scoring_service = SemanticScoringService(resume_repo, jd_repo, config_repo, campaign_candidate_repo)
        breakdown = scoring_service.calculate_and_store_semantic_score_breakdown(
            campaign_candidate.id, campaign.jd_id, campaign_candidate.resume_id,
        )

        rejection_reason = None
        if not breakdown["semantic_passed"]:
            # T02-style: one human-readable reason, never a raw field name/
            # UUID. Recorded on every semantic failure (not just the first),
            # same convention as deterministic scoring's candidate_rejections.
            rejection_reason = breakdown["semantic_explanation"]
            candidate_rejection_repo.create(CandidateRejection(
                campaign_candidate_id=campaign_candidate.id,
                rejection_layer=RejectionLayer.SEMANTIC,
                rejection_reason=rejection_reason,
                rejection_detail=breakdown,
            ))

            # SCREENING -> REJECTED, validated against allowed_transitions -
            # exactly the same StageTransitionService deterministic scoring
            # already uses, extended to SEMANTIC by design (see that
            # service's own docstring: "so any future rejection layer
            # (SEMANTIC, AI) reuses the exact same validate-then-apply
            # behavior instead of re-implementing it").
            stage_transition_service.transition_to_rejected(
                campaign_candidate,
                change_reason="Semantic similarity filter rejection",
                scores_snapshot=breakdown,
            )

            # A candidate rejected at the SEMANTIC layer must never have a
            # queued AI_EVALUATE task run against them either - reused as-is
            # from deterministic_scoring_tasks.py, fully generic (no
            # deterministic-specific behavior inside it).
            _cancel_downstream_ai_evaluation(
                campaign_candidate, task_log_repo, task_log_service, campaign_candidate_repo,
            )

        summary_payload = {
            "semantic_score": breakdown["semantic_score"],
            "semantic_passed": breakdown["semantic_passed"],
            "semantic_threshold": breakdown["semantic_threshold"],
            "matching_skills_count": len(breakdown["matching_skills"]),
            "missing_skills_count": len(breakdown["missing_skills"]),
            "rejection_reason": rejection_reason,
            "semantic_score_breakdown": breakdown,
        }

        # Shares this task's db session - flushed here, committed together
        # with the campaign_candidate/rejection writes below, same
        # convention as calculate_deterministic_score_task's audit call.
        audit_service.log(
            actor_id=None,
            actor_role="SYSTEM",
            action_type=ActionType.SEMANTIC_SCORE_COMPUTED,
            entity_type=EntityType.CAMPAIGN_CANDIDATE,
            entity_id=campaign_candidate.id,
            campaign_id=campaign.id,
            details=summary_payload,
        )

        campaign_candidate_repo.commit()

        task_log_service.mark_success(task_log, summary=json.dumps(summary_payload))

        # M09: only after the transaction above has committed - a candidate
        # that just passed semantic screening is auto-enqueued for AI
        # evaluation, the terminal screening stage. Mirrors exactly how
        # calculate_deterministic_score_task enqueues semantic scoring
        # after its own pass (same local-import-inside-the-if-branch shape,
        # kept consistent even though ai_evaluation_tasks.py doesn't import
        # back from this module). A failure here must never crash or mask
        # the already-successful semantic outcome, same reasoning as that
        # enqueue call.
        if breakdown["semantic_passed"]:
            try:
                from app.tasks.ai_evaluation_tasks import _enqueue_ai_evaluation
                _enqueue_ai_evaluation(campaign_candidate, task_log_service)
            except Exception:
                logger.exception(
                    "Failed to enqueue AI evaluation after semantic pass for campaign_candidate_id=%s",
                    campaign_candidate.id,
                )

    except Exception as ex:
        db.rollback()
        classification = classify(ex)

        if classification != FailureClassification.PERMANENT and attempt_number < _SEMANTIC_SCORE_RETRY_POLICY.max_attempts:
            if task_log:
                task_log_service.mark_retry(task_log)
            delay = compute_backoff_seconds(_SEMANTIC_SCORE_RETRY_POLICY, attempt_number)
            logger.warning(
                "Semantic scoring transient failure, retrying | campaign_candidate_id=%s attempt=%s delay=%ss error=%s",
                campaign_candidate_id, attempt_number, delay, ex,
            )
            self.retry(exc=ex, countdown=delay, max_retries=_SEMANTIC_SCORE_RETRY_POLICY.max_attempts)
            return

        # Retries exhausted (or a permanent failure) - dead-letter, mark the
        # task_log DEAD, log the failure reason. Never re-raised: this is
        # now dead-lettered/terminal bookkeeping, same convention as
        # generate_resume_embedding_task.
        error_message = str(ex)
        try:
            DeadLetterQueueRepository(db).create(
                original_task_id=task_id,
                task_type=SEMANTIC_SCORE_TASK_TYPE,
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
                "Failed to dead-letter semantic scoring for campaign_candidate_id=%s", campaign_candidate_id,
            )
            db.rollback()

        if task_log:
            task_log_service.mark_dead(task_log, error_message)
        logger.exception("Semantic scoring task permanently failed for campaign_candidate_id %s", campaign_candidate_id)

    finally:
        db.close()
