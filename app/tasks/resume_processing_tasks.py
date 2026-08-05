import json
import logging
from uuid import UUID, uuid4

from app.core.celery_app import celery_app
from app.db.session import SessionLocal

from app.models.async_tasks import DocumentType
from app.models.candidates import FileFormat
from app.models.resume.resume_source_format import ResumeSourceFormat
from app.repositories.audit_repository import AuditRepository
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.checkpoint_repository import CheckpointRepository
from app.repositories.dead_letter_queue_repository import DeadLetterQueueRepository
from app.repositories.document_processing_repository import DocumentProcessingRepository
from app.repositories.prompt_template_repository import PromptTemplateRepository
from app.repositories.resume_repository import ResumeRepository
from app.repositories.skill_repository import SkillRepository
from app.repositories.stage_failure_log_repository import StageFailureLogRepository
from app.tasks.deterministic_scoring_tasks import (
    DETERMINISTIC_SCORE_TASK_TYPE,
    calculate_deterministic_score_task,
)

from app.services.ai.embedding_service import EmbeddingService
from app.services.ai.preprocessing_service import PreprocessingService
from app.services.audit_service import AuditService
from app.services.celery_task_log_service import CeleryTaskLogService
from app.services.document_processing.retry_driver import RetryDriver
from app.services.document_processing.stage_execution_service import StageExecutionError, StageExecutionService
from app.services.extractions.gemini_extraction_service import GeminiExtractionService
from app.services.jd.hash_service import HashService
from app.services.pii.pii_detection_service import PIIDetectionService
from app.services.pii.pii_redaction_service import PIIRedactionService
from app.services.resume.resume_processing_pipeline import ResumeProcessingPipeline
from app.services.resume.resume_service import ResumeService
from app.services.skills.skill_normalization_service import SkillNormalizationService
from app.core.storage_service import StorageService
from app.tasks.embedding_tasks import _enqueue_resume_embedding

logger = logging.getLogger(__name__)

RESUME_DOCUMENT_PROCESSING_TASK_TYPE = "RESUME_DOCUMENT_PROCESSING"

# Resume.file_format (FileFormat) also allows PNG/JPEG for scanned/image
# resumes — out of scope here, same as the rest of this pipeline: no OCR
# path exists yet, only the PDF/DOCX text-extraction path ResumeSourceFormat
# models.
_FILE_FORMAT_TO_SOURCE_FORMAT = {
    FileFormat.PDF: ResumeSourceFormat.PDF,
    FileFormat.DOCX: ResumeSourceFormat.DOCX,
}


def _enqueue_deterministic_scoring(db, resume_id, task_log_service: CeleryTaskLogService) -> None:
    """
    M07-E01 S02: after skill normalization's candidate_skills have
    committed (i.e. this resume's processing pipeline has fully
    succeeded), queue DETERMINISTIC_SCORE for every campaign_candidate
    this resume belongs to. A plain apply_async - independent from and
    unchained to anything else queued (e.g. resume-level embedding
    generation, which already ran synchronously earlier in this same
    pipeline) - so it runs on its own, in parallel with whatever else is
    queued.

    Idempotency: keyed on (campaign_candidate_id, resume_id), checked via
    CeleryTaskLog.idempotency_key before enqueueing, so a retried
    process_resume_document run (or a re-run against the same resume)
    never double-queues scoring for the same candidate+resume.
    """
    campaign_candidate_repo = CampaignCandidateRepository(db)
    logger.warning(
    "=== ENTERED _enqueue_deterministic_scoring === resume_id=%s",
    resume_id,
)
    task_log_repo = task_log_service.repository

    for campaign_candidate in campaign_candidate_repo.get_by_resume_id(resume_id):
        idempotency_key = f"{DETERMINISTIC_SCORE_TASK_TYPE}:{campaign_candidate.id}:{resume_id}"

        if task_log_repo.get_by_idempotency_key(idempotency_key) is not None:
            logger.info(
                "Deterministic scoring already queued/run for campaign_candidate_id=%s resume_id=%s - skipping.",
                campaign_candidate.id, resume_id,
            )
            continue

        scoring_task_id = str(uuid4())
        task_log_service.create_log(
            task_id=scoring_task_id,
            task_type=DETERMINISTIC_SCORE_TASK_TYPE,
            idempotency_key=idempotency_key,
            resume_id=resume_id,
            campaign_candidate_id=campaign_candidate.id,
        )

        try:
            calculate_deterministic_score_task.apply_async(
                kwargs={"campaign_candidate_id": str(campaign_candidate.id)},
                task_id=scoring_task_id,
            )
        except Exception:
            logger.exception(
                "Failed to enqueue deterministic scoring for campaign_candidate_id=%s resume_id=%s",
                campaign_candidate.id, resume_id,
            )


@celery_app.task(name="resume.process_document", bind=True)
def process_resume_document(self, resume_id: str, prompt_template_id: str) -> None:
    """
    Background Resume document-processing pipeline: Text Extraction ->
    Text Cleaning -> AI Extraction -> JSON Validation -> Skill Normalization
    -> Persistence. Mirrors process_jd_document's structure, minus the
    file-upload/storage-download setup JD does inline in the route — the
    Resume and its file_path already exist by the time this task runs
    (Candidate/Resume creation is out of scope for this pipeline).

    M08-E01: embedding generation is no longer a pipeline stage here — once
    the pipeline succeeds, EMBED_RESUME is enqueued the same way
    DETERMINISTIC_SCORE already is (see _enqueue_resume_embedding).

    Stage tracking runs on its own session (`stage_db`), separate from the
    business-write session (`db`), same reasoning as process_jd_document.
    """
    db = SessionLocal()
    stage_db = SessionLocal()
    task_log = None
    retry_driver = None
    attempt_number = 1
    resume = None
    task_id = self.request.id
    logger.warning(
    "=== PROCESS_RESUME_DOCUMENT STARTED === resume_id=%s task_id=%s",
    resume_id,
    task_id,
)
    try:
        resume_repo = ResumeRepository(db)
        skill_repo = SkillRepository(db)
        stage_repo = DocumentProcessingRepository(stage_db)
        audit_repo = AuditRepository(db)
        task_log_repo = CeleryTaskLogRepository(db)

        audit_service = AuditService(audit_repo)
        task_log_service = CeleryTaskLogService(task_log_repo)
        stage_tracker = StageExecutionService(stage_repo)

        existing_task_log = task_log_repo.get_by_task_id(task_id)
        if existing_task_log is None:
            existing_task_log = task_log_service.create_log(
                task_id=task_id,
                task_type=RESUME_DOCUMENT_PROCESSING_TASK_TYPE,
            )
        task_log = task_log_service.mark_running(existing_task_log)

        resume = resume_repo.get_by_id(resume_id)
        if resume is None:
            raise ValueError(f"Resume with ID {resume_id} not found.")

        source_format = _FILE_FORMAT_TO_SOURCE_FORMAT.get(resume.file_format)
        if source_format is None:
            raise ValueError(
                f"Resume {resume_id} has file_format {resume.file_format}, "
                "which this pipeline does not support (only PDF/DOCX)."
            )

        resume_service = ResumeService(
            repository=resume_repo,
            audit_service=audit_service,
        )

        embedding_service = EmbeddingService(db)

        checkpoint_repo = CheckpointRepository(stage_db)
        stage_failure_log_repo = StageFailureLogRepository(stage_db)
        dead_letter_queue_repo = DeadLetterQueueRepository(db)
        retry_driver = RetryDriver(
            checkpoint_repo,
            stage_failure_log_repo,
            dead_letter_queue_repo,
            task_log_service,
            task_log,
            task_type=RESUME_DOCUMENT_PROCESSING_TASK_TYPE,
        )

        pipeline = ResumeProcessingPipeline(
            preprocessing_service=PreprocessingService(),
            extraction_service=GeminiExtractionService(),
            storage_service=StorageService(),
            skill_normalization_service=SkillNormalizationService(skill_repo, embedding_service),
            resume_service=resume_service,
            resume_repository=resume_repo,
            skill_repository=skill_repo,
            stage_tracker=stage_tracker,
            pii_detection_service=PIIDetectionService(),
            pii_redaction_service=PIIRedactionService(),
            prompt_template_repository=PromptTemplateRepository(db),
        )

        attempt_number = self.request.retries + 1
        logger.warning("=== CALLING pipeline.run() === resume_id=%s task_id=%s", resume_id, task_id)
        processed_resume_id = pipeline.run(
            task_id=task_id,
            resume_id=resume.id,
            candidate_id=resume.candidate_id,
            file_path=resume.file_path,
            source_format=source_format,
            prompt_template_id=UUID(prompt_template_id),
            attempt_number=attempt_number,
        )
        logger.warning("=== pipeline.run() RETURNED === resume_id=%s", processed_resume_id)

        task_log.resume_id = processed_resume_id
        task_log_repo.update(task_log)
        task_log_repo.commit()
        task_log_service.mark_success(task_log, summary=f"Resume {processed_resume_id} parsed.")

        # M08-E01: enqueued BEFORE deterministic scoring, not after -
        # calculate_deterministic_score_task's own auto-trigger calls
        # _enqueue_semantic_scoring internally as soon as it finishes, and
        # that check requires the resume embedding to already exist. With
        # a sequential worker (e.g. Windows solo pool), whichever of these
        # two is enqueued first is also the one that finishes first, so
        # enqueueing deterministic scoring first meant it reliably ran (and
        # took its "no resume embedding yet" fallback) before EMBED_RESUME
        # had even started - leaving semantic scoring dependent entirely on
        # the later trigger_pending_semantic_scoring_for_resume catch-up
        # (called from generate_resume_embedding_task) or the 15-minute
        # recover_pending_semantic_scores Beat job to pick it back up. A
        # failure here must never affect the already-committed resume
        # processing result.
        try:
            _enqueue_resume_embedding(db, processed_resume_id, task_log_service)
        except Exception:
            logger.exception(
                "Failed to enqueue resume embedding after resume %s parsed.", processed_resume_id,
            )

        # Resume processing has already fully succeeded and committed above
        # - a failure enqueueing deterministic scoring must never overwrite
        # that success (or crash this task); log and move on.
        try:
            _enqueue_deterministic_scoring(db, processed_resume_id, task_log_service)
        except Exception:
            logger.exception(
                "Failed to enqueue deterministic scoring after resume %s parsed.", processed_resume_id,
            )

    except StageExecutionError as stage_exc:
        should_retry = False
        if retry_driver is not None:
            should_retry = retry_driver.handle_failure(
                self,
                task_id,
                DocumentType.RESUME,
                stage_exc,
                attempt_number,
                # Epic 4 (M05-E04) Phase D10 - lets a dead-lettered resume
                # failure be traced back to its resume for replay.
                resume_id=resume.id if resume is not None else None,
            )
        if not should_retry:
            db.rollback()
            if resume is not None:
                try:
                    resume_repo = ResumeRepository(db)
                    resume_repo.mark_parse_failed(resume)
                    resume_repo.commit()
                except Exception:
                    logger.exception("Failed to mark resume %s parse_status=FAILED.", resume_id)
                    db.rollback()
            if task_log:
                task_log_service.mark_failure(task_log, str(stage_exc.original))
            logger.exception("Resume document processing task failed for task_id %s", task_id)
            raise stage_exc.original
    except Exception as ex:
        db.rollback()
        if resume is not None:
            # Otherwise a resume whose file_format has no OCR/parse path
            # (or any other pre-pipeline failure) is left at
            # parse_status=PENDING forever instead of a visible terminal
            # state — never let this bookkeeping mask the real exception.
            try:
                resume_repo = ResumeRepository(db)
                resume_repo.mark_parse_failed(resume)
                resume_repo.commit()
            except Exception:
                logger.exception("Failed to mark resume %s parse_status=FAILED.", resume_id)
                db.rollback()
        if task_log:
            task_log_service.mark_failure(task_log, str(ex))
        logger.exception("Resume document processing task failed for task_id %s", task_id)
        raise

    finally:
        db.close()
        stage_db.close()


RESUME_UPLOAD_RECOVERY_TASK_TYPE = "RESUME_UPLOAD_RECOVERY_SCAN"


def recover_stalled_resume_uploads(db=None) -> int:
    """
    Resume-upload resilience: redispatches every RESUME_DOCUMENT_PROCESSING
    celery_task_log row whose apply_async() call itself failed at enqueue
    time (dispatch_failed=True - the broker was unreachable, so nothing
    was ever actually queued). Deliberately never touches
    dispatch_failed=False rows - those already reached the broker and are
    just waiting for a worker; process_resume_document has no
    SUCCESS-shortcut, so redispatching an already-queued message would
    reprocess the same resume twice.

    Callable two ways:
    - Directly, with its own SessionLocal (db=None) - used by the FastAPI
      startup hook, which must work even if Celery/Redis themselves are
      still unreachable (dispatching this AS a Celery task would defeat
      the point).
    - Via recover_stalled_resume_uploads_task below (Celery Beat), passing
      that task's own db session.

    claim_for_redispatch's atomic UPDATE...WHERE means two concurrent
    calls (a Beat tick racing the startup scan, or two app instances
    starting at once) never redispatch the same row twice.

    Returns the number of tasks successfully redispatched.
    """
    owns_session = db is None
    if owns_session:
        db = SessionLocal()
    try:
        task_log_repo = CeleryTaskLogRepository(db)
        task_log_service = CeleryTaskLogService(task_log_repo)
        campaign_candidate_repo = CampaignCandidateRepository(db)
        campaign_repo = CampaignRepository(db)

        stalled = task_log_repo.get_queued_dispatch_failed(RESUME_DOCUMENT_PROCESSING_TASK_TYPE)
        recovered = 0

        for task_log in stalled:
            if not task_log_repo.claim_for_redispatch(task_log.id):
                logger.info(
                    "Resume upload recovery skipped | task_id=%s reason=already_claimed", task_log.task_id,
                )
                continue

            campaign_candidates = campaign_candidate_repo.get_by_resume_id(task_log.resume_id)
            campaign = (
                campaign_repo.get_by_id(campaign_candidates[0].campaign_id)
                if campaign_candidates else None
            )
            if campaign is None:
                logger.error(
                    "Resume upload recovery failed | task_id=%s resume_id=%s reason=campaign_not_found",
                    task_log.task_id, task_log.resume_id,
                )
                task_log_service.mark_dispatch_failed(task_log, "Campaign not found for resume_id.")
                continue

            try:
                process_resume_document.apply_async(
                    kwargs={
                        "resume_id": str(task_log.resume_id),
                        "prompt_template_id": str(campaign.prompt_template_id),
                    },
                    task_id=task_log.task_id,
                )
                recovered += 1
                logger.info(
                    "Recovery dispatched task | task_id=%s resume_id=%s", task_log.task_id, task_log.resume_id,
                )
            except Exception as exc:
                logger.exception(
                    "Resume upload recovery failed | task_id=%s resume_id=%s reason=queue_unavailable",
                    task_log.task_id, task_log.resume_id,
                )
                task_log_service.mark_dispatch_failed(task_log, str(exc))

        return recovered
    finally:
        if owns_session:
            db.close()


@celery_app.task(name="resume.recover_stalled_uploads")
def recover_stalled_resume_uploads_task() -> None:
    """Celery Beat entry point - periodic safety net alongside the FastAPI startup scan."""
    db = SessionLocal()
    task_log = None
    try:
        task_log_repo = CeleryTaskLogRepository(db)
        task_log_service = CeleryTaskLogService(task_log_repo)
        task_log = task_log_service.create_log(
            task_id=str(uuid4()),
            task_type=RESUME_UPLOAD_RECOVERY_TASK_TYPE,
        )

        recovered = recover_stalled_resume_uploads(db)

        summary = json.dumps({"recovered": recovered})
        task_log_service.mark_success(task_log, summary=summary)
        logger.info("Resume upload recovery scan completed | recovered=%s", recovered)

    except Exception as ex:
        db.rollback()
        if task_log:
            task_log_service.mark_failure(task_log, str(ex))
        logger.exception("Resume upload recovery scan failed")

    finally:
        db.close()
