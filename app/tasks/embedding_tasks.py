import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.core.celery_app import celery_app
from app.db.session import SessionLocal
from app.models.async_tasks import CeleryTaskLog, FailureClassification, TaskStatus
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.dead_letter_queue_repository import DeadLetterQueueRepository
from app.repositories.resume_repository import ResumeRepository
from app.services.ai.embedding_service import EmbeddingService
from app.services.celery_task_log_service import CeleryTaskLogService
from app.services.document_processing.error_classifier import classify
from app.services.document_processing.retry_policy import RetryPolicy, compute_backoff_seconds
from app.services.resume.anonymized_embedding_text_builder import (
    build_anonymized_embedding_text,
    verify_anonymized_text,
)

logger = logging.getLogger(__name__)

EMBED_RESUME_TASK_TYPE = "EMBED_RESUME"

# M08-E01 T06: platform_config key controlling SentenceTransformer batch
# size - read fresh per task run (never hardcoded), falls back to this
# default only when unset/unreachable.
_EMBEDDING_BATCH_SIZE_KEY = "EMBEDDING_BATCH_SIZE"
_DEFAULT_EMBEDDING_BATCH_SIZE = 32

# M08-E01: a small, independent retry policy for a single-resume embedding
# run - deliberately NOT RetryDriver (coupled to the multi-stage document-
# processing pipeline's StageExecutionError/checkpoint model, which
# doesn't fit this one-step task), same reasoning/shape as email_tasks.py's
# _EMAIL_RETRY_POLICY.
_EMBED_RESUME_RETRY_POLICY = RetryPolicy(max_attempts=3, base_delay_seconds=10, max_delay_seconds=120)


def _dead_letter_and_mark_dead(
    db, task_id, resume_id, resume_uuid, attempt_number, task_log, task_log_service, error_message,
) -> None:
    """
    Shared terminal-failure path: every non-retryable EMBED_RESUME outcome
    (retries exhausted, a permanent classification, or a failed
    anonymisation-verification check) lands here, so dead_letter_queue is
    always the single source of truth for "this resume's embedding needs
    manual attention" - never split across some failures that dead-letter
    and others that only flip celery_task_log to a terminal status.
    """
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
    """
    M08-E01: queues EMBED_RESUME for this resume, called once resume
    processing (parsing + skill normalization + persistence) has fully
    succeeded - mirrors resume_processing_tasks.py's
    _enqueue_deterministic_scoring exactly, except idempotency is keyed on
    resume_id alone (not per campaign_candidate): an embedding is a
    property of the resume itself, reusable across however many campaigns
    that resume is later attached to, not recomputed per assignment.

    Production-readiness fix: the pre-check below (get_by_idempotency_key)
    still narrows the common case, but two concurrent callers can both pass
    it before either commits. uq_celery_task_log_idempotency_key backs the
    actual insert at the DB level, so create_if_new_idempotency_key's
    was_created=False path is the real guarantee - apply_async is skipped
    whenever another caller already won the race, never just logged.
    """
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


def _read_embedding_batch_size(config_repo: ConfigRepository) -> int:
    raw = config_repo.get_configs_by_keys([_EMBEDDING_BATCH_SIZE_KEY]).get(_EMBEDDING_BATCH_SIZE_KEY)
    if raw is None:
        return _DEFAULT_EMBEDDING_BATCH_SIZE
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid EMBEDDING_BATCH_SIZE platform_config value %r - falling back to default %s.",
            raw, _DEFAULT_EMBEDDING_BATCH_SIZE,
        )
        return _DEFAULT_EMBEDDING_BATCH_SIZE


@celery_app.task(name="embedding.generate_resume_embedding", bind=True)
def generate_resume_embedding_task(self, resume_id: str) -> None:
    """
    M08-E01 Phase 1: Construct Anonymised Input -> Verify Anonymisation ->
    Deduplication Check -> Embedding Generation (only if no dup found) ->
    Store VECTOR(384) -> Update Celery Logs.

    Independent of deterministic scoring/campaign outcomes - runs to
    completion regardless of what happens to any campaign_candidate this
    resume is attached to (mirrors the EMBED_RESUME-is-independent
    reasoning already documented in deterministic_scoring_tasks.py's
    _cancel_downstream_ai_evaluation).
    """
    db = SessionLocal()
    task_log = None
    task_id = self.request.id
    attempt_number = self.request.retries + 1
    started_at = time.monotonic()
    resume_uuid = UUID(resume_id)
    try:
        resume_repo = ResumeRepository(db)
        config_repo = ConfigRepository(db)
        task_log_repo = CeleryTaskLogRepository(db)
        task_log_service = CeleryTaskLogService(task_log_repo)

        existing_task_log = task_log_repo.get_by_task_id(task_id)
        # A broker redelivery of this exact task_id after it already ran to
        # completion must never re-embed or insert a second resume_embeddings
        # row for the same run. A FAILURE/RUNNING log is still reprocessed
        # (the work never actually finished), only SUCCESS short-circuits.
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
            # Task 2: verification failed - stop processing, dead-letter,
            # mark DEAD. Never generates an embedding. Routed through the
            # same dead-letter path as every other non-retryable failure
            # below, so this is never invisible to DLQ-based monitoring.
            _dead_letter_and_mark_dead(
                db, task_id, resume_id, resume_uuid, attempt_number, task_log, task_log_service,
                anonymization_failure_reason,
            )
            logger.error(
                "Embedding generation stopped - anonymisation verification failed | "
                "resume_id=%s reason=%s", resume_id, anonymization_failure_reason,
            )
            return

        # Task 4: active model only, never hardcoded - raises RuntimeError
        # if none is configured (an infra/config problem, not this
        # resume's fault).
        embedding_model_version = resume_repo.get_active_embedding_model_version()

        # Task 3: MD5 of the anonymised text - input_text itself is never
        # stored anywhere, only its hash.
        input_text_hash = hashlib.md5(input_text.encode("utf-8")).hexdigest()

        # Task 5: dedup check before ever calling the embedding service.
        existing_embedding = resume_repo.get_embedding_by_hash(
            input_text_hash, embedding_model_version.id,
        )

        if existing_embedding is not None:
            # Copy the matched row's anonymisation/talent-pool flags rather
            # than relying on create_resume_embedding's defaults, so a
            # correction made on the source row (e.g. is_talent_pool_eligible
            # flipped for a compliance reason) propagates to every reused copy.
            _, was_created = resume_repo.create_resume_embedding(
                resume_id=resume.id,
                candidate_id=resume.candidate_id,
                embedding=existing_embedding.embedding,
                embedding_model_version_id=embedding_model_version.id,
                input_text_hash=input_text_hash,
                is_anonymized=existing_embedding.is_anonymized,
                is_talent_pool_eligible=existing_embedding.is_talent_pool_eligible,
            )
            vector_action = "VECTOR_REUSED"
        else:
            # Task 6: batch-capable call (even though this task only ever
            # embeds one text) - the same generate_embeddings() a future
            # multi-resume backfill/batch task would call, so batch
            # support lives in one place, not duplicated per call site.
            batch_size = _read_embedding_batch_size(config_repo)
            embedding_vectors = EmbeddingService().generate_embeddings([input_text], batch_size=batch_size)

            # Task 7. uq_resume_embeddings_resume_model_version is the final
            # backstop against a concurrent duplicate: was_created is False
            # when another worker won this exact (resume_id,
            # embedding_model_version_id) race between our dedup-by-hash
            # check above and this insert - report it as reused, since that
            # is what actually happened, never as a second generation.
            _, was_created = resume_repo.create_resume_embedding(
                resume_id=resume.id,
                candidate_id=resume.candidate_id,
                embedding=embedding_vectors[0],
                embedding_model_version_id=embedding_model_version.id,
                input_text_hash=input_text_hash,
            )
            vector_action = "VECTOR_GENERATED" if was_created else "VECTOR_REUSED"

        resume_repo.commit()

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

    except Exception as ex:
        db.rollback()
        classification = classify(ex)

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

        # Retries exhausted (or a permanent failure) - dead-letter, mark
        # the task_log DEAD, log the failure reason. Never re-raised: this
        # is now dead-lettered/terminal bookkeeping, not an unhandled
        # Celery-level failure (same convention as send_candidate_email_task).
        error_message = str(ex)
        _dead_letter_and_mark_dead(
            db, task_id, resume_id, resume_uuid, attempt_number, task_log, task_log_service, error_message,
        )
        logger.exception("EMBED_RESUME permanently failed | resume_id=%s", resume_id)

    finally:
        db.close()
