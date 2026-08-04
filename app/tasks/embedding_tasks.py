import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from uuid import UUID, uuid4

from celery.exceptions import Retry

from app.core.celery_app import celery_app
from app.db.session import SessionLocal
from app.models.async_tasks import CeleryTaskLog, FailureClassification, TaskStatus
from app.models.config import CBState
from app.models.pipeline import AIEvaluationStatus
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.circuit_breaker_repository import CircuitBreakerRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.dead_letter_queue_repository import DeadLetterQueueRepository
from app.repositories.resume_repository import ResumeRepository
from app.services.ai.embedding_service import EmbeddingService
from app.services.celery_task_log_service import CeleryTaskLogService
from app.services.document_processing.error_classifier import classify
from app.services.document_processing.embedding_error_classifier import (
    EmbeddingFailureClassification,
    classify_embedding_error,
)
from app.services.document_processing.retry_policy import RetryPolicy, compute_backoff_seconds
from app.services.resume.anonymized_embedding_text_builder import (
    build_anonymized_embedding_text,
    verify_anonymized_text,
)

logger = logging.getLogger(__name__)

EMBED_RESUME_TASK_TYPE = "EMBED_RESUME"

EMBEDDING_SERVICE_NAME = "EMBEDDING_SERVICE"

_EMBEDDING_BATCH_SIZE_KEY = "EMBEDDING_BATCH_SIZE"
_DEFAULT_EMBEDDING_BATCH_SIZE = 32

_MAX_EMBED_RETRY_COUNT_KEY = "MAX_EMBED_RETRY_COUNT"
_EMBED_RETRY_BASE_DELAY_SECONDS_KEY = "EMBED_RETRY_BASE_DELAY_SECONDS"
_EMBED_RETRY_MAX_DELAY_SECONDS_KEY = "EMBED_RETRY_MAX_DELAY_SECONDS"
_EMBEDDING_CIRCUIT_BREAKER_FAILURE_THRESHOLD_KEY = "EMBEDDING_CIRCUIT_BREAKER_FAILURE_THRESHOLD"
_DEFAULT_MAX_EMBED_RETRY_COUNT = 4
_DEFAULT_EMBED_RETRY_BASE_DELAY_SECONDS = 30
_DEFAULT_EMBED_RETRY_MAX_DELAY_SECONDS = 240
_DEFAULT_EMBEDDING_CIRCUIT_BREAKER_FAILURE_THRESHOLD = 10


_CELERY_MAX_RETRIES_CEILING = 1000

_EMBED_RESUME_RETRY_POLICY = RetryPolicy(max_attempts=3, base_delay_seconds=10, max_delay_seconds=120)


def _read_int_config(config_repo: ConfigRepository, key: str, default: int) -> int:
    raw = config_repo.get_configs_by_keys([key]).get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid %s platform_config value %r - falling back to default %s.",
            key, raw, default,
        )
        return default


def _read_embedding_batch_size(config_repo: ConfigRepository) -> int:
    return _read_int_config(config_repo, _EMBEDDING_BATCH_SIZE_KEY, _DEFAULT_EMBEDDING_BATCH_SIZE)


def _read_embed_retry_policy(config_repo: ConfigRepository) -> RetryPolicy:
    
    return RetryPolicy(
        max_attempts=_read_int_config(config_repo, _MAX_EMBED_RETRY_COUNT_KEY, _DEFAULT_MAX_EMBED_RETRY_COUNT),
        base_delay_seconds=_read_int_config(
            config_repo, _EMBED_RETRY_BASE_DELAY_SECONDS_KEY, _DEFAULT_EMBED_RETRY_BASE_DELAY_SECONDS,
        ),
        max_delay_seconds=_read_int_config(
            config_repo, _EMBED_RETRY_MAX_DELAY_SECONDS_KEY, _DEFAULT_EMBED_RETRY_MAX_DELAY_SECONDS,
        ),
    )


def _set_manual_review_for_resume_candidates(db, resume_uuid: UUID) -> int:
   
    campaign_candidate_repo = CampaignCandidateRepository(db)
    affected = campaign_candidate_repo.get_by_resume_id(resume_uuid)
    for campaign_candidate in affected:
        campaign_candidate.ai_evaluation_status = AIEvaluationStatus.MANUAL_REVIEW
        campaign_candidate_repo.update(campaign_candidate)
    campaign_candidate_repo.commit()
    return len(affected)


def _dead_letter_and_mark_dead(
    db, task_id, resume_id, resume_uuid, attempt_number, task_log, task_log_service, error_message,
) -> None:
    
    try:
        DeadLetterQueueRepository(db).create(
            original_task_id=task_id,
            task_type=EMBED_RESUME_TASK_TYPE,
            final_error_message=error_message,
            full_error_trace=None,
            input_payload={"resume_id": resume_id},
            retry_count=attempt_number,
            first_attempted_at=task_log.queued_at if task_log else datetime.now(timezone.utc),
            last_attempted_at=datetime.now(timezone.utc),
            resume_id=resume_uuid,
        )
        DeadLetterQueueRepository(db).commit()
    except Exception:
        logger.exception("Failed to dead-letter EMBED_RESUME for resume_id=%s", resume_id)
        db.rollback()

    if task_log:
        task_log_service.mark_dead(task_log, error_message)


def _enqueue_resume_embedding(db, resume_id, task_log_service: CeleryTaskLogService) -> None:
   
    task_log_repo = task_log_service.repository
    idempotency_key = f"{EMBED_RESUME_TASK_TYPE}:{resume_id}"

    if task_log_repo.get_by_idempotency_key(idempotency_key) is not None:
        logger.info(
            "Embedding generation already queued/run for resume_id=%s - skipping.", resume_id,
        )
        return

    embedding_task_id = str(uuid4())
    log = CeleryTaskLog(
        task_id=embedding_task_id,
        task_type=EMBED_RESUME_TASK_TYPE,
        idempotency_key=idempotency_key,
        resume_id=resume_id,
        status=TaskStatus.QUEUED,
    )
    _, was_created = task_log_repo.create_if_new_idempotency_key(log)
    task_log_repo.commit()

    if not was_created:
        logger.info(
            "Embedding generation already queued/run for resume_id=%s "
            "(race detected at insert) - skipping.", resume_id,
        )
        return

    try:
        generate_resume_embedding_task.apply_async(
            kwargs={"resume_id": str(resume_id)},
            task_id=embedding_task_id,
        )
    except Exception:
        logger.exception("Failed to enqueue EMBED_RESUME for resume_id=%s", resume_id)


@celery_app.task(name="embedding.generate_resume_embedding", bind=True)
def generate_resume_embedding_task(self, resume_id: str) -> None:
   
    db = SessionLocal()
    task_log = None
    task_id = self.request.id
    started_at = time.monotonic()
    resume_uuid = UUID(resume_id)
    try:
        resume_repo = ResumeRepository(db)
        config_repo = ConfigRepository(db)
        task_log_repo = CeleryTaskLogRepository(db)
        task_log_service = CeleryTaskLogService(task_log_repo)
        cb_repo = CircuitBreakerRepository(db)

        existing_task_log = task_log_repo.get_by_task_id(task_id)
        
        if existing_task_log is not None and existing_task_log.status == TaskStatus.SUCCESS:
            logger.info(
                "Embedding generation already completed for task_id=%s resume_id=%s - skipping duplicate run.",
                task_id, resume_id,
            )
            return

        resume = resume_repo.get_by_id(resume_uuid)

        if existing_task_log is None:
            existing_task_log = task_log_service.create_log(
                task_id=task_id,
                task_type=EMBED_RESUME_TASK_TYPE,
                # Only set when the resume genuinely exists - same FK-safety
                # reasoning as calculate_deterministic_score_task's
                # campaign_candidate existence check.
                resume_id=resume.id if resume is not None else None,
            )
        task_log = task_log_service.mark_running(existing_task_log)

        if resume is None:
            summary = json.dumps({"skipped": True, "reason": f"Resume {resume_id} no longer exists."})
            task_log_service.mark_success(task_log, summary=summary)
            logger.warning("Embedding generation skipped | resume_id=%s reason=resume_deleted", resume_id)
            return

        parsed_json = resume.parsed_json
        if not parsed_json or not isinstance(parsed_json, dict):
            raise ValueError(f"Resume {resume_id} has no parsed_json - cannot construct an embedding input.")

        input_text = build_anonymized_embedding_text(parsed_json)
        if not input_text:
            raise ValueError(
                f"Resume {resume_id} has no skills, work experience, or education in parsed_json - "
                "nothing available to embed."
            )

        is_anonymized_text, anonymization_failure_reason = verify_anonymized_text(input_text)
        if not is_anonymized_text:

            _dead_letter_and_mark_dead(
                db, task_id, resume_id, resume_uuid, 1, task_log, task_log_service,
                anonymization_failure_reason,
            )
            _set_manual_review_for_resume_candidates(db, resume_uuid)
            logger.error(
                "Embedding generation stopped - anonymisation verification failed | "
                "resume_id=%s reason=%s", resume_id, anonymization_failure_reason,
            )
            return

        
        embedding_model_version = resume_repo.get_active_embedding_model_version()

        input_text_hash = hashlib.md5(input_text.encode("utf-8")).hexdigest()


        existing_embedding = resume_repo.get_embedding_by_hash(
            input_text_hash, embedding_model_version.id,
        )

        if existing_embedding is not None:
            # Talent Pool Eligibility: is_talent_pool_eligible is
            # deliberately NEVER copied from the matched row (unlike
            # is_anonymized, which is a property of the text/vector
            # itself) - eligibility is a property of THIS candidate, not
            # of whichever unrelated candidate happened to produce an
            # identical anonymised-text hash. "On successful resume
            # embedding, set is_talent_pool_eligible = TRUE" applies
            # unconditionally here too - any disqualifying condition for
            # this specific candidate is corrected afterward by the daily
            # validate_talent_pool_eligibility reconciliation task, never
            # inferred from a different candidate's embedding row.
            _, was_created = resume_repo.create_resume_embedding(
                resume_id=resume.id,
                candidate_id=resume.candidate_id,
                embedding=existing_embedding.embedding,
                embedding_model_version_id=embedding_model_version.id,
                input_text_hash=input_text_hash,
                is_anonymized=existing_embedding.is_anonymized,
            )
            vector_action = "VECTOR_REUSED"
        else:

            failure_threshold = _read_int_config(
                config_repo, _EMBEDDING_CIRCUIT_BREAKER_FAILURE_THRESHOLD_KEY,
                _DEFAULT_EMBEDDING_CIRCUIT_BREAKER_FAILURE_THRESHOLD,
            )
            cb_repo.get_or_create(EMBEDDING_SERVICE_NAME, failure_threshold=failure_threshold)
            cb_state = cb_repo.transition_to_half_open_if_due(EMBEDDING_SERVICE_NAME)

            if cb_state.state == CBState.OPEN:
                if task_log:
                    task_log.status = TaskStatus.RETRY
                    task_log_repo.update(task_log)
                    task_log_repo.commit()
                countdown = (
                    max((cb_state.retry_after - datetime.now(timezone.utc)).total_seconds(), 1.0)
                    if cb_state.retry_after else 60.0
                )
                logger.warning(
                    "EMBED_RESUME circuit breaker OPEN for %s - not calling the embedding service, "
                    "rescheduling | resume_id=%s countdown=%ss",
                    EMBEDDING_SERVICE_NAME, resume_id, countdown,
                )
                # Raises celery.exceptions.Retry - deliberately re-raised
                # unchanged by the `except Retry: raise` clause below
                # rather than being treated as a real embedding failure.
                self.retry(countdown=countdown, max_retries=_CELERY_MAX_RETRIES_CEILING)
                return
            try:
                batch_size = _read_embedding_batch_size(config_repo)
                embedding_vectors = EmbeddingService().generate_embeddings([input_text], batch_size=batch_size)
            except Exception as embed_ex:
                db.rollback()
                cb_repo.increment_failure(EMBEDDING_SERVICE_NAME)
                cb_repo.commit()

                failure_info = classify_embedding_error(embed_ex)
                retry_policy = _read_embed_retry_policy(config_repo)
              
                embed_attempt_number = (task_log.retry_count if task_log else 0) + 1

                if (
                    failure_info.classification != EmbeddingFailureClassification.PERMANENT
                    and embed_attempt_number < retry_policy.max_attempts
                ):
                    if task_log:
                        task_log_service.mark_retry(task_log)

                    if (
                        failure_info.classification == EmbeddingFailureClassification.RATE_LIMITED
                        and failure_info.retry_after_seconds is not None
                    ):
                        delay = failure_info.retry_after_seconds
                    else:
                        delay = compute_backoff_seconds(retry_policy, embed_attempt_number)

                    logger.warning(
                        "EMBED_RESUME %s failure, retrying | resume_id=%s attempt=%s delay=%ss error=%s",
                        failure_info.classification.value, resume_id, embed_attempt_number, delay, embed_ex,
                    )
                   
                    embed_ex._embed_retry_already_handled = True
                    self.retry(exc=embed_ex, countdown=delay, max_retries=_CELERY_MAX_RETRIES_CEILING)
                    return
                error_message = str(embed_ex)
                _dead_letter_and_mark_dead(
                    db, task_id, resume_id, resume_uuid, embed_attempt_number,
                    task_log, task_log_service, error_message,
                )
                _set_manual_review_for_resume_candidates(db, resume_uuid)
                logger.exception("EMBED_RESUME permanently failed | resume_id=%s", resume_id)
                return

            cb_repo.reset(EMBEDDING_SERVICE_NAME)
            cb_repo.commit()

          
            _, was_created = resume_repo.create_resume_embedding(
                resume_id=resume.id,
                candidate_id=resume.candidate_id,
                embedding=embedding_vectors[0],
                embedding_model_version_id=embedding_model_version.id,
                input_text_hash=input_text_hash,
            )
            vector_action = "VECTOR_GENERATED" if was_created else "VECTOR_REUSED"

        resume_repo.commit()

        try:
            from app.tasks.semantic_scoring_tasks import trigger_pending_semantic_scoring_for_resume
            trigger_pending_semantic_scoring_for_resume(db, resume.id)
        except Exception:
            logger.exception(
                "Failed to trigger pending semantic scoring after embedding for resume_id=%s", resume_id,
            )

        # Task 8
        processing_time_ms = round((time.monotonic() - started_at) * 1000, 2)
        summary_payload = {
            "action": vector_action,
            "resume_id": str(resume.id),
            "embedding_model": f"{embedding_model_version.model_name}:{embedding_model_version.model_version}",
            "anonymization_result": "PASSED",
            "processing_time_ms": processing_time_ms,
        }
        task_log_service.mark_success(task_log, summary=json.dumps(summary_payload))
        logger.info(
            "%s | resume_id=%s model=%s processing_time_ms=%s",
            vector_action, resume.id, summary_payload["embedding_model"], processing_time_ms,
        )

    except Retry:
       
        raise

    except Exception as ex:
        if getattr(ex, "_embed_retry_already_handled", False):

           
            raise

    
        db.rollback()
        classification = classify(ex)
        attempt_number = self.request.retries + 1

        if classification != FailureClassification.PERMANENT and attempt_number < _EMBED_RESUME_RETRY_POLICY.max_attempts:
            if task_log:
                task_log_service.mark_retry(task_log)
            delay = compute_backoff_seconds(_EMBED_RESUME_RETRY_POLICY, attempt_number)
            logger.warning(
                "EMBED_RESUME transient failure, retrying | resume_id=%s attempt=%s delay=%ss error=%s",
                resume_id, attempt_number, delay, ex,
            )
            self.retry(exc=ex, countdown=delay, max_retries=_EMBED_RESUME_RETRY_POLICY.max_attempts)
            return


        error_message = str(ex)
        _dead_letter_and_mark_dead(
            db, task_id, resume_id, resume_uuid, attempt_number, task_log, task_log_service, error_message,
        )
        _set_manual_review_for_resume_candidates(db, resume_uuid)
        logger.exception("EMBED_RESUME permanently failed | resume_id=%s", resume_id)

    finally:
        db.close()
