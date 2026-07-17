import logging

from app.core.celery_app import celery_app
from app.db.session import SessionLocal

from app.models.async_tasks import DocumentType
from app.models.candidates import FileFormat
from app.models.resume.resume_source_format import ResumeSourceFormat
from app.repositories.audit_repository import AuditRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.checkpoint_repository import CheckpointRepository
from app.repositories.dead_letter_queue_repository import DeadLetterQueueRepository
from app.repositories.document_processing_repository import DocumentProcessingRepository
from app.repositories.resume_repository import ResumeRepository
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
from app.services.resume.resume_processing_pipeline import ResumeProcessingPipeline
from app.services.resume.resume_service import ResumeService
from app.services.skills.skill_normalization_service import SkillNormalizationService
from app.core.storage_service import StorageService

logger = logging.getLogger(__name__)


# Resume.file_format (FileFormat) also allows PNG/JPEG for scanned/image
# resumes — out of scope here, same as the rest of this pipeline: no OCR
# path exists yet, only the PDF/DOCX text-extraction path ResumeSourceFormat
# models.
_FILE_FORMAT_TO_SOURCE_FORMAT = {
    FileFormat.PDF: ResumeSourceFormat.PDF,
    FileFormat.DOCX: ResumeSourceFormat.DOCX,
}


@celery_app.task(name="resume.process_document", bind=True)
def process_resume_document(self, resume_id: str) -> None:
    """
    Background Resume document-processing pipeline: Text Extraction ->
    Text Cleaning -> AI Extraction -> JSON Validation -> Skill Normalization
    -> Embedding Generation -> Persistence. Mirrors process_jd_document's
    structure, minus the file-upload/storage-download setup JD does inline
    in the route — the Resume and its file_path already exist by the time
    this task runs (Candidate/Resume creation is out of scope for this
    pipeline).

    Stage tracking runs on its own session (`stage_db`), separate from the
    business-write session (`db`), same reasoning as process_jd_document.
    """
    db = SessionLocal()
    stage_db = SessionLocal()
    task_log = None
    retry_driver = None
    attempt_number = 1
    task_id = self.request.id
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
                task_type="RESUME_DOCUMENT_PROCESSING",
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

        embedding_service = EmbeddingService()

        checkpoint_repo = CheckpointRepository(stage_db)
        stage_failure_log_repo = StageFailureLogRepository(stage_db)
        dead_letter_queue_repo = DeadLetterQueueRepository(db)
        retry_driver = RetryDriver(
            checkpoint_repo,
            stage_failure_log_repo,
            dead_letter_queue_repo,
            task_log_service,
            task_log,
        )

        pipeline = ResumeProcessingPipeline(
            preprocessing_service=PreprocessingService(),
            extraction_service=GeminiExtractionService(),
            hash_service=HashService(),
            storage_service=StorageService(),
            skill_normalization_service=SkillNormalizationService(skill_repo, embedding_service),
            embedding_service=embedding_service,
            resume_service=resume_service,
            resume_repository=resume_repo,
            skill_repository=skill_repo,
            stage_tracker=stage_tracker,
        )

        attempt_number = self.request.retries + 1
        processed_resume_id = pipeline.run(
            task_id=task_id,
            resume_id=resume.id,
            candidate_id=resume.candidate_id,
            file_path=resume.file_path,
            source_format=source_format,
            attempt_number=attempt_number,
        )

        task_log.resume_id = processed_resume_id
        task_log_repo.update(task_log)
        task_log_repo.commit()
        task_log_service.mark_success(task_log, summary=f"Resume {processed_resume_id} parsed.")

    except StageExecutionError as stage_exc:
        should_retry = False
        if retry_driver is not None:
            should_retry = retry_driver.handle_failure(
                self,
                task_id,
                DocumentType.RESUME,
                stage_exc,
                attempt_number,
            )
        if not should_retry:
            if task_log:
                task_log_service.mark_failure(task_log, str(stage_exc.original))
            logger.exception("Resume document processing task failed for task_id %s", task_id)
            raise stage_exc.original
    except Exception as ex:
        db.rollback()
        if task_log:
            task_log_service.mark_failure(task_log, str(ex))
        logger.exception("Resume document processing task failed for task_id %s", task_id)
        raise

    finally:
        db.close()
        stage_db.close()
