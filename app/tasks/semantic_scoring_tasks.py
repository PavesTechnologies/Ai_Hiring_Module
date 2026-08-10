import hashlib
import json
import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

from celery.exceptions import Retry

from app.core.celery_app import celery_app
from app.db.session import SessionLocal
from app.enums.constants import ActionType, EntityType
from app.models.async_tasks import CeleryTaskLog, FailureClassification, TaskStatus
from app.models.campaigns import CampaignStatus
from app.models.pipeline import AIEvaluationStatus, DecisionSource
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.allowed_transition_repository import AllowedTransitionRepository
from app.repositories.audit_repository import AuditRepository
from app.repositories.campaign_candidate_ai_evaluation_repository import CampaignCandidateAIEvaluationRepository
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.dead_letter_queue_repository import DeadLetterQueueRepository
from app.repositories.jd_repository import JDRepository
from app.repositories.resume_repository import ResumeRepository
from app.services.audit_service import AuditService
from app.services.campaign.semantic_scoring_service import SemanticScoringService
from app.services.campaign.stage_transition_service import StageTransitionService
from app.services.celery_task_log_service import CeleryTaskLogService
from app.services.document_processing.error_classifier import classify
from app.services.document_processing.retry_policy import RetryPolicy, compute_backoff_seconds
from app.tasks.deterministic_scoring_tasks import (
    _cancel_downstream_ai_evaluation,
    _queue_rejection_email,
)

logger = logging.getLogger(__name__)


SEMANTIC_SCORE_TASK_TYPE = "SEMANTIC_SCORE"


_SCOREABLE_CAMPAIGN_STATUSES = {CampaignStatus.ACTIVE, CampaignStatus.PAUSED}


_SEMANTIC_SCORE_RETRY_POLICY = RetryPolicy(max_attempts=3, base_delay_seconds=10, max_delay_seconds=120)

# Exact reason text required for the "resume embedding not available" skip
# path - recorded verbatim on the candidate_rejections row so it's both
# human-readable and machine-matchable (idempotency check below).
MISSING_RESUME_EMBEDDING_REASON = "Resume embedding not available — semantic scoring skipped"

# Task 536: JD embedding pre-flight - retry on a flat interval (distinct
# from _SEMANTIC_SCORE_RETRY_POLICY's exponential backoff, which is for
# unexpected/transient exceptions, not this deliberate pre-check) before
# giving up and routing to MANUAL_REVIEW.
_JD_EMBEDDING_MAX_RETRIES = 3
_JD_EMBEDDING_RETRY_DELAY_SECONDS = 60
JD_EMBEDDING_NOT_FOUND_REASON = "JD_EMBEDDING_NOT_FOUND"
MODEL_VERSION_MISMATCH_REASON = "MODEL_VERSION_MISMATCH"


def _score_and_persist_semantic(
    campaign_candidate,
    campaign,
    resume_repo: ResumeRepository,
    jd_repo: JDRepository,
    ai_evaluation_repo: CampaignCandidateAIEvaluationRepository,
    stage_transition_service: StageTransitionService,
    campaign_candidate_repo: CampaignCandidateRepository,
    audit_service: AuditService,
    task_log_repo: CeleryTaskLogRepository,
    task_log_service: CeleryTaskLogService,
) -> dict:

    scoring_service = SemanticScoringService(resume_repo, jd_repo, campaign_candidate_repo)
    breakdown = scoring_service.calculate_and_store_semantic_score_breakdown(
        campaign_candidate.id, campaign.jd_id, campaign_candidate.resume_id,
        semantic_threshold=float(campaign.semantic_threshold),
    )

    rejection_reason = None
    stage_transition_succeeded = False
    if breakdown["semantic_passed"]:
        # Story 541: PASS -> queue AI_EVALUATE (never for a rejected candidate).
        # Lazy import mirrors deterministic_scoring_tasks._enqueue_semantic_scoring's
        # own cross-task chaining convention - avoids a circular import
        # (ai_evaluation_tasks imports deterministic_scoring_tasks at module load).
        from app.tasks.ai_evaluation_tasks import _enqueue_ai_evaluation

        _enqueue_ai_evaluation(campaign_candidate, task_log_service)
    else:
        rejection_reason = breakdown["semantic_explanation"]

        stage_transition_succeeded = stage_transition_service.transition_to_rejected(
            campaign_candidate,
            change_reason="Semantic similarity filter rejection",
            scores_snapshot=breakdown,
            decision_source=DecisionSource.SEMANTIC,
            decision_reason=rejection_reason,
            decision_details=breakdown,
        )

        _cancel_downstream_ai_evaluation(
            campaign_candidate, task_log_repo, task_log_service, ai_evaluation_repo,
        )

    summary_payload = {
        "semantic_score": breakdown["semantic_score"],
        "semantic_passed": breakdown["semantic_passed"],
        "semantic_threshold": breakdown["semantic_threshold"],
        "matching_skills_count": len(breakdown["matching_skills"]),
        "missing_skills_count": len(breakdown["missing_skills"]),
        "rejection_reason": rejection_reason,
        "semantic_score_breakdown": breakdown,
        # Story 538/540: surfaced at the top level (not just nested in
        # semantic_score_breakdown) so the caller can record duration_ms on
        # celery_task_log without re-parsing the breakdown.
        "computation_duration_ms": breakdown["computation_duration_ms"],
        "score_clamped_to_zero": breakdown["score_clamped_to_zero"],
        "score_clamp_reason": breakdown["score_clamp_reason"],
    }

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

    # Story 542: only after the transaction above has committed - never
    # send a rejection email for a candidate whose pipeline_stage didn't
    # actually move to REJECTED (transition blocked - see stage_transition_succeeded
    # above). Reuses the exact same helper the deterministic layer uses -
    # it swallows its own failures, so a delivery problem here never masks
    # the already-successful scoring outcome or blocks this task.
    if not breakdown["semantic_passed"] and stage_transition_succeeded:
        _queue_rejection_email(campaign_candidate_repo.db, campaign_candidate)

    return summary_payload


def trigger_pending_semantic_scoring_for_resume(db, resume_id) -> None:

    # TEMPORARY DIAGNOSTIC LOGGING (enqueue-trigger investigation) - remove
    # once the missing-SEMANTIC_SCORE-row issue is confirmed resolved.
    logger.info("TRACE: trigger_pending_semantic_scoring_for_resume entered | resume_id=%s", resume_id)

    campaign_candidate_repo = CampaignCandidateRepository(db)
    resume_repo = ResumeRepository(db)
    task_log_repo = CeleryTaskLogRepository(db)
    task_log_service = CeleryTaskLogService(task_log_repo)

    all_candidates = campaign_candidate_repo.get_by_resume_id(resume_id)
    logger.info(
        "TRACE: get_by_resume_id found %s campaign_candidate(s) | resume_id=%s",
        len(all_candidates), resume_id,
    )
    pending_candidates = []
    for cc in all_candidates:
        if cc.deterministic_passed and cc.semantic_breakdown is None:
            pending_candidates.append(cc)
        else:
            logger.info(
                "TRACE: campaign_candidate_id=%s skipped | deterministic_passed=%s "
                "semantic_score_breakdown_is_none=%s",
                cc.id, cc.deterministic_passed, cc.semantic_breakdown is None,
            )
    logger.info(
        "TRACE: %s pending candidate(s) eligible for semantic scoring | resume_id=%s",
        len(pending_candidates), resume_id,
    )
    for campaign_candidate in pending_candidates:
        logger.info(
            "TRACE: calling _enqueue_semantic_scoring | campaign_candidate_id=%s", campaign_candidate.id,
        )
        _enqueue_semantic_scoring(campaign_candidate, task_log_service, resume_repo)


def _semantic_score_idempotency_key(campaign_candidate_id) -> str:
    """
    Task 535: idempotency_key = hash(campaign_candidate_id + "SEM") - one
    stable identity per candidate's SEMANTIC_SCORE celery_task_log row,
    backed by uq_celery_task_log_idempotency_key. A re-trigger (HR
    override, recovery scan) after a prior terminal (FAILURE/DEAD) attempt
    reuses and resets this SAME row rather than inserting a second one -
    the partial unique index would otherwise reject a second insert with
    the same key.
    """
    return hashlib.sha256(f"{campaign_candidate_id}SEM".encode("utf-8")).hexdigest()


def _dispatch_semantic_score_task(campaign_candidate, log: CeleryTaskLog) -> None:
    try:
        calculate_semantic_score_task.apply_async(
            kwargs={"campaign_candidate_id": str(campaign_candidate.id)},
            task_id=log.task_id,
        )
        logger.info(
            "Semantic scoring queued | campaign_candidate_id=%s task_id=%s",
            campaign_candidate.id, log.task_id,
        )
    except Exception:
        logger.exception(
            "Failed to enqueue SEMANTIC_SCORE for campaign_candidate_id=%s", campaign_candidate.id,
        )


def _enqueue_semantic_scoring(
    campaign_candidate,
    task_log_service: CeleryTaskLogService,
    resume_repo: ResumeRepository,
    jd_id=None,
) -> None:

    # TEMPORARY DIAGNOSTIC LOGGING (enqueue-trigger investigation) - remove
    # once the missing-SEMANTIC_SCORE-row issue is confirmed resolved.
    logger.info(
        "TRACE: _enqueue_semantic_scoring entered | campaign_candidate_id=%s resume_id=%s",
        campaign_candidate.id, campaign_candidate.resume_id,
    )

    if resume_repo.get_embedding(campaign_candidate.resume_id) is None:
        logger.info(
            "Semantic scoring enqueue skipped | campaign_candidate_id=%s reason=no_resume_embedding_yet",
            campaign_candidate.id,
        )
        # Task 537: give the candidate a path forward instead of silently
        # stalling forever - EMBED_RESUME's own idempotency_key makes this
        # a no-op if it's already queued/run for this resume.
        # trigger_pending_semantic_scoring_for_resume picks this candidate
        # back up once that embedding actually completes.
        try:
            from app.tasks.embedding_tasks import _enqueue_resume_embedding
            _enqueue_resume_embedding(
                task_log_service.repository.db, campaign_candidate.resume_id, task_log_service,
            )
        except Exception:
            logger.exception(
                "Failed to enqueue EMBED_RESUME fallback for campaign_candidate_id=%s",
                campaign_candidate.id,
            )
        return

    task_log_repo = task_log_service.repository
    idempotency_key = _semantic_score_idempotency_key(campaign_candidate.id)
    new_task_id = str(uuid4())

    existing_log = task_log_repo.get_by_idempotency_key(idempotency_key)
    if existing_log is None:
        candidate_log = CeleryTaskLog(
            task_id=new_task_id,
            task_type=SEMANTIC_SCORE_TASK_TYPE,
            idempotency_key=idempotency_key,
            campaign_candidate_id=campaign_candidate.id,
            jd_id=jd_id,
            status=TaskStatus.QUEUED,
        )
        log, was_created = task_log_repo.create_if_new_idempotency_key(candidate_log)
        task_log_repo.commit()
        if was_created:
            logger.info(
                "TRACE: celery_task_log row created (task_id=%s) BEFORE dispatch | campaign_candidate_id=%s",
                new_task_id, campaign_candidate.id,
            )
            _dispatch_semantic_score_task(campaign_candidate, log)
            return
        existing_log = log  # lost the race - another caller's row already exists

    if existing_log.status in (TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.SUCCESS):
        logger.info(
            "Semantic scoring enqueue skipped | campaign_candidate_id=%s reason=already_queued_or_scored",
            campaign_candidate.id,
        )
        return

    # A terminal (FAILURE/DEAD) row for this candidate already exists - a
    # legitimate re-trigger (HR override, recovery scan). Reuse the same
    # idempotency-keyed row (with a fresh Celery task_id) rather than
    # inserting a second one, which the unique index would reject.
    existing_log.task_id = new_task_id
    existing_log.status = TaskStatus.QUEUED
    log = task_log_repo.update(existing_log)
    task_log_repo.commit()
    _dispatch_semantic_score_task(campaign_candidate, log)


@celery_app.task(name="scoring.calculate_semantic_score", bind=True)
def calculate_semantic_score_task(self, campaign_candidate_id: str) -> None:
   
    db = SessionLocal()
    task_log = None
    task_id = self.request.id
    attempt_number = self.request.retries + 1
    try:
        campaign_candidate_repo = CampaignCandidateRepository(db)
        campaign_repo = CampaignRepository(db)
        resume_repo = ResumeRepository(db)
        jd_repo = JDRepository(db)
        ai_evaluation_repo = CampaignCandidateAIEvaluationRepository(db)
        allowed_transition_repo = AllowedTransitionRepository(db)
        audit_service = AuditService(AuditRepository(db))
        task_log_repo = CeleryTaskLogRepository(db)
        task_log_service = CeleryTaskLogService(task_log_repo)
        stage_transition_service = StageTransitionService(allowed_transition_repo, campaign_candidate_repo, audit_service)

        campaign_candidate = campaign_candidate_repo.get_by_id(UUID(campaign_candidate_id))

        existing_task_log = task_log_repo.get_by_task_id(task_id)
       
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

        logger.info(
            "Semantic scoring task started | campaign_candidate_id=%s task_id=%s",
            campaign_candidate_id, task_id,
        )

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

        resume_embedding = resume_repo.get_embedding(campaign_candidate.resume_id)
        if resume_embedding is None:
            # Requirement: a missing resume embedding is a graceful skip,
            # never a retry-then-dead-letter failure (that's what this task
            # used to do via MissingResumeEmbeddingError, before
            # recover_pending_semantic_scores existed to pick this
            # candidate back up automatically once EMBED_RESUME eventually
            # succeeds for this resume). semantic_score is left NULL
            # (already its default - never set to anything here),
            # ai_evaluation_status flags MANUAL_REVIEW so this shows up as
            # needing attention, and decision_source/decision_reason record
            # why - but pipeline_stage is deliberately left untouched (no
            # stage_transition_service call at all): this is NOT an
            # automatic rejection of the candidate, just a record of why no
            # semantic score exists yet.
            ai_evaluation = ai_evaluation_repo.get_or_create(campaign_candidate.id)
            ai_evaluation.ai_evaluation_status = AIEvaluationStatus.MANUAL_REVIEW
            ai_evaluation_repo.update(ai_evaluation)

            # Idempotent: if this candidate was already re-queued once
            # before the embedding was ready (e.g. an HR override
            # re-triggered it) and hit this exact skip again, don't
            # re-stamp an identical decision - decision_reason IS the
            # candidate's current-state snapshot now, so a direct field
            # check replaces the old rejection-history lookup.
            if campaign_candidate.decision_reason != MISSING_RESUME_EMBEDDING_REASON:
                campaign_candidate.decision_source = DecisionSource.SEMANTIC
                campaign_candidate.decision_reason = MISSING_RESUME_EMBEDDING_REASON
                campaign_candidate.decision_details = {"reason": MISSING_RESUME_EMBEDDING_REASON}
                campaign_candidate.decision_at = datetime.now(timezone.utc)
                campaign_candidate_repo.update(campaign_candidate)

            campaign_candidate_repo.commit()

            summary = json.dumps({
                "skipped": True,
                "reason": MISSING_RESUME_EMBEDDING_REASON,
                "semantic_score": None,
            })
            task_log_service.mark_success(task_log, summary=summary)
            logger.info(
                "Semantic scoring skipped | campaign_candidate_id=%s reason=no_resume_embedding",
                campaign_candidate_id,
            )
            return

        # Task 536: JD embedding pre-flight - retried on a flat 60s
        # interval (distinct from the exponential-backoff exception path
        # below), since a JD embedding can legitimately still be in
        # flight (EMBED_JD queued but not yet complete) when semantic
        # scoring first runs.
        jd_embedding = jd_repo.get_embedding_by_jd_id(campaign.jd_id)
        if jd_embedding is None:
            if self.request.retries < _JD_EMBEDDING_MAX_RETRIES:
                task_log_service.mark_retry(task_log)
                logger.info(
                    "JD embedding not found, scheduling retry | campaign_candidate_id=%s jd_id=%s attempt=%s",
                    campaign_candidate_id, campaign.jd_id, self.request.retries + 1,
                )
                self.retry(countdown=_JD_EMBEDDING_RETRY_DELAY_SECONDS, max_retries=_JD_EMBEDDING_MAX_RETRIES)
                return

            campaign_candidate.semantic_score = None
            campaign_candidate_repo.update(campaign_candidate)
            ai_evaluation = ai_evaluation_repo.get_or_create(campaign_candidate.id)
            ai_evaluation.ai_evaluation_status = AIEvaluationStatus.MANUAL_REVIEW
            ai_evaluation_repo.update(ai_evaluation)
            campaign_candidate_repo.commit()

            summary = json.dumps({
                "skipped": True,
                "reason": JD_EMBEDDING_NOT_FOUND_REASON,
                "semantic_score": None,
            })
            task_log_service.mark_success(task_log, summary=summary)
            logger.warning(
                "%s | campaign_candidate_id=%s jd_id=%s retries_exhausted=%s",
                JD_EMBEDDING_NOT_FOUND_REASON, campaign_candidate_id, campaign.jd_id, _JD_EMBEDDING_MAX_RETRIES,
            )
            return

        # Task 536: model-version-mismatch pre-flight - a resume embedded
        # under one EmbeddingModelVersion is not comparable to a JD
        # embedded under another; route to MANUAL_REVIEW rather than
        # silently computing a meaningless similarity score.
        if resume_embedding.embedding_model_version_id != jd_embedding.embedding_model_version_id:
            campaign_candidate.semantic_score = None
            campaign_candidate_repo.update(campaign_candidate)
            ai_evaluation = ai_evaluation_repo.get_or_create(campaign_candidate.id)
            ai_evaluation.ai_evaluation_status = AIEvaluationStatus.MANUAL_REVIEW
            ai_evaluation_repo.update(ai_evaluation)
            campaign_candidate_repo.commit()

            summary = json.dumps({
                "skipped": True,
                "reason": MODEL_VERSION_MISMATCH_REASON,
                "semantic_score": None,
            })
            task_log_service.mark_success(task_log, summary=summary)
            logger.warning(
                "%s | campaign_candidate_id=%s resume_model_version_id=%s jd_model_version_id=%s",
                MODEL_VERSION_MISMATCH_REASON, campaign_candidate_id,
                resume_embedding.embedding_model_version_id, jd_embedding.embedding_model_version_id,
            )
            return

        # All pre-flight validations passed - recorded in celery_task_log
        # before the similarity calculation itself runs.
        task_log.output_summary = json.dumps({
            "validations": {
                "resume_embedding_found": True,
                "jd_embedding_found": True,
                "model_versions_match": True,
            },
        })
        task_log_repo.update(task_log)
        task_log_repo.commit()
        logger.info(
            "Semantic scoring pre-flight validations passed | campaign_candidate_id=%s", campaign_candidate_id,
        )

        summary_payload = _score_and_persist_semantic(
            campaign_candidate, campaign, resume_repo, jd_repo,
            ai_evaluation_repo, stage_transition_service, campaign_candidate_repo,
            audit_service, task_log_repo, task_log_service,
        )

        # Task 538: computation duration recorded on this same task_log row -
        # mark_success() persists whatever is currently set on task_log, so
        # this must be assigned before that call.
        task_log.duration_ms = summary_payload["computation_duration_ms"]

        task_log_service.mark_success(task_log, summary=json.dumps(summary_payload))

        logger.info(
            "Semantic scoring task completed | campaign_candidate_id=%s semantic_passed=%s",
            campaign_candidate_id, summary_payload["semantic_passed"],
        )

    except Retry:
        # Task 536's JD-embedding-missing pre-flight retry (self.retry()
        # with no exc= raises Retry directly) - a scheduled retry, not a
        # business failure. Must never fall into the generic
        # classify()/dead-letter handling below.
        raise

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
        logger.info(
            "Semantic scoring task failed | campaign_candidate_id=%s task_id=%s", campaign_candidate_id, task_id,
        )
        logger.exception("Semantic scoring task permanently failed for campaign_candidate_id %s", campaign_candidate_id)

    finally:
        db.close()


SEMANTIC_SCORE_RECOVERY_TASK_TYPE = "SEMANTIC_SCORE_RECOVERY_SCAN"


@celery_app.task(name="scoring.recover_pending_semantic_scores")
def recover_pending_semantic_scores() -> None:
    """
    Requirement 4 (automatic recovery), run periodically via Celery Beat:
    calculate_semantic_score_task's own missing-resume-embedding skip path
    (see MISSING_RESUME_EMBEDDING_REASON above) deliberately never retries
    itself anymore - it succeeds-with-skip immediately, on the assumption
    that this scan is what eventually catches the candidate back up once
    EMBED_RESUME actually succeeds for that resume (a later retry, or a
    manually resolved permanent failure).

    Re-enqueues via the existing _enqueue_semantic_scoring helper (never
    executes semantic scoring business logic itself) - that helper's own
    idempotency check (a QUEUED/RUNNING/SUCCESS SEMANTIC_SCORE
    celery_task_log row already existing) means a candidate that was
    somehow already re-queued by something else in the meantime is safely
    skipped, never double-dispatched.
    """
    db = SessionLocal()
    task_log = None
    try:
        campaign_candidate_repo = CampaignCandidateRepository(db)
        resume_repo = ResumeRepository(db)
        task_log_repo = CeleryTaskLogRepository(db)
        task_log_service = CeleryTaskLogService(task_log_repo)

        task_log = task_log_service.create_log(
            task_id=str(uuid4()),
            task_type=SEMANTIC_SCORE_RECOVERY_TASK_TYPE,
        )

        pending_candidates = campaign_candidate_repo.get_pending_semantic_score_with_ready_embedding()
        for campaign_candidate in pending_candidates:
            _enqueue_semantic_scoring(campaign_candidate, task_log_service, resume_repo)

        summary = json.dumps({
            "candidates_found": len(pending_candidates),
        })
        task_log_service.mark_success(task_log, summary=summary)
        logger.info(
            "Semantic score recovery scan completed | candidates_found=%s", len(pending_candidates),
        )

    except Exception as ex:
        db.rollback()
        if task_log:
            task_log_service.mark_failure(task_log, str(ex))
        logger.exception("Semantic score recovery scan failed")

    finally:
        db.close()
