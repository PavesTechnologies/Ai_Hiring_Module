import logging
from uuid import UUID

from app.core.celery_app import celery_app
from app.db.session import SessionLocal

from app.models.async_tasks import DocumentType
from app.repositories.audit_repository import AuditRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.checkpoint_repository import CheckpointRepository
from app.repositories.dead_letter_queue_repository import DeadLetterQueueRepository
from app.repositories.document_processing_repository import DocumentProcessingRepository
from app.repositories.jd_repository import JDRepository
from app.repositories.prompt_template_repository import PromptTemplateRepository
from app.repositories.skill_repository import SkillRepository
from app.repositories.stage_failure_log_repository import StageFailureLogRepository

from app.services.ai.embedding_service import EmbeddingService
from app.services.ai.preprocessing_service import PreprocessingService
from app.services.audit_service import AuditService
from app.services.celery_task_log_service import CeleryTaskLogService
from app.services.document_processing.retry_driver import RetryDriver
from app.services.document_processing.stage_execution_service import StageExecutionError, StageExecutionService
from app.services.extractions.gemini_extraction_service import GeminiExtractionService
from app.services.jd.hash_service import HashService
from app.services.jd.jd_processing_pipeline import JDProcessingPipeline
from app.services.jd.jd_service import JDService
from app.services.skills.skill_normalization_service import SkillNormalizationService
from app.core.storage_service import StorageService

logger = logging.getLogger(__name__)


@celery_app.task(name="jd.process_document", bind=True)
def process_jd_document(
    self,
    task_id: str,
    raw_text: str | None,
    file_path: str | None,
    title: str,
    jurisdiction: str,
    min_experience_years: float | None,
    education_criteria: dict | None,
    created_by: str,
    prompt_template_id: str,
    max_experience_years: float | None = None,
    notice_period: int | None = None,
    existing_jd_id: str | None = None,
    version_number: int = 1,
    parent_jd_id: str | None = None,
    lineage_root_id: str | None = None,
    old_file_path: str | None = None,
    original_filename: str | None = None,
) -> None:
    
    db = SessionLocal()
    stage_db = SessionLocal()
    task_log = None
    checkpoint_repo = None
    retry_driver = None
    attempt_number = 1
    try:
        jd_repo = JDRepository(db)
        skill_repo = SkillRepository(db)
        stage_repo = DocumentProcessingRepository(stage_db)
        audit_repo = AuditRepository(db)
        task_log_repo = CeleryTaskLogRepository(db)

        audit_service = AuditService(audit_repo)
        task_log_service = CeleryTaskLogService(task_log_repo)
        stage_tracker = StageExecutionService(stage_repo)

        existing_task_log = task_log_repo.get_by_task_id(task_id)
        if existing_task_log is None:
            # Fallback only — the route now creates this row synchronously
            # (with created_by/title) before queuing the task, so this
            # branch should just cover callers that queue the task directly.
            existing_task_log = task_log_service.create_log(
                task_id=task_id,
                task_type="JD_DOCUMENT_PROCESSING",
            )
        task_log = task_log_service.mark_running(existing_task_log)

        jd_service = JDService(
            repository=jd_repo,
            hash_service=HashService(),
            audit_service=audit_service,
            storage_service=StorageService(),
            prompt_template_repository=PromptTemplateRepository(db),
        )

        # One EmbeddingService instance shared by the pipeline's own JD-level
        # embedding stage and by skill-level semantic matching — the
        # underlying sentence-transformer model is a class-level singleton
        # either way, but there's no reason to instantiate the wrapper twice.
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
            task_type="JD_DOCUMENT_PROCESSING",
        )

        pipeline = JDProcessingPipeline(
            preprocessing_service=PreprocessingService(),
            extraction_service=GeminiExtractionService(),
            hash_service=HashService(),
            storage_service=StorageService(),
            skill_normalization_service=SkillNormalizationService(skill_repo, embedding_service),
            embedding_service=embedding_service,
            jd_service=jd_service,
            jd_repository=jd_repo,
            skill_repository=skill_repo,
            stage_tracker=stage_tracker,
            checkpoint_repo=checkpoint_repo,
        )

        attempt_number = self.request.retries + 1
        jd_id = pipeline.run(
            task_id=task_id,
            raw_text=raw_text,
            file_path=file_path,
            original_filename=original_filename,
            title=title,
            jurisdiction=jurisdiction,
            min_experience_years=min_experience_years,
            max_experience_years=max_experience_years,
            notice_period=notice_period,
            education_criteria=education_criteria,
            created_by=created_by,
            prompt_template_id=UUID(prompt_template_id),
            existing_jd_id=UUID(existing_jd_id) if existing_jd_id else None,
            version_number=version_number,
            parent_jd_id=UUID(parent_jd_id) if parent_jd_id else None,
            lineage_root_id=UUID(lineage_root_id) if lineage_root_id else None,
            attempt_number=attempt_number,
        )

        active_checkpoint = checkpoint_repo.get(task_id)
        if active_checkpoint is not None:
            checkpoint_repo.delete(task_id)
            checkpoint_repo.commit()

        task_log.jd_id = jd_id
        task_log_repo.update(task_log)
        task_log_repo.commit()
        task_log_service.mark_success(
            task_log,
            summary=f"JD {jd_id} reprocessed." if existing_jd_id else f"JD {jd_id} created.",
        )
        if existing_jd_id and file_path and old_file_path:
            try:
                jd_service.storage_service.delete_file(
                    bucket_name=jd_service.JD_STORAGE_BUCKET,
                    file_path=old_file_path,
                )
            except Exception:
                logger.exception(
                    "Failed to delete superseded JD document '%s' for JD %s.",
                    old_file_path, jd_id,
                )
        try:
            from app.services.embedding_queue_service import EmbeddingQueueService, JDEmbeddingQueueError
            EmbeddingQueueService().queue_jd_embedding(jd_id, force_regenerate=False)
        except JDEmbeddingQueueError:
            logger.exception("Failed to enqueue EMBED_JD after JD creation/reprocess for jd_id=%s", jd_id)

    except StageExecutionError as stage_exc:
        should_retry = False
        if retry_driver is not None:
            should_retry = retry_driver.handle_failure(
                self,
                task_id,
                DocumentType.JD,
                stage_exc,
                attempt_number,
            )
        if not should_retry:
            if task_log:
                task_log_service.mark_failure(task_log, str(stage_exc.original))
            logger.exception("JD document processing task failed for task_id %s", task_id)
            raise stage_exc.original
    except Exception as ex:
        
        db.rollback()
        if task_log:
            task_log_service.mark_failure(task_log, str(ex))
        logger.exception("JD document processing task failed for task_id %s", task_id)
        raise

    finally:
        db.close()
        stage_db.close()
