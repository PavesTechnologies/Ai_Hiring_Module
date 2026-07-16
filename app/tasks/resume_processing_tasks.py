import logging
from uuid import UUID

from app.core.celery_app import celery_app
from app.core.storage_service import StorageService
from app.db.session import SessionLocal
from app.models.candidates import FileFormat
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.document_processing_repository import DocumentProcessingRepository
from app.repositories.resume_repository import ResumeRepository
from app.services.ai.preprocessing_service import PreprocessingService
from app.services.celery_task_log_service import CeleryTaskLogService
from app.services.document_processing.stage_execution_service import StageExecutionService
from app.services.extractions.gemini_resume_extraction_service import GeminiResumeExtractionService
from app.services.resume.resume_processing_pipeline import ResumeProcessingPipeline

logger = logging.getLogger(__name__)


@celery_app.task(name="resume.process_document")
def process_resume_document(
    task_id: str,
    resume_id: str,
    candidate_id: str,
    file_path: str,
    file_format: str,
) -> None:
    """
    Background leg of the resume processing pipeline (everything after
    Validation/Storage/candidate-creation, which already ran synchronously
    in the upload route — see
    app/services/resume/resume_intake_service.py): Text Extraction ->
    Text Cleaning -> AI Extraction -> JSON Validation -> Persistence.

    Mirrors process_jd_document's dual-session structure: stage tracking
    runs on its own session (stage_db), separate from the business-write
    session (db), so StageExecutionService's frequent per-stage commits
    never finalize pending business writes early.
    """
    db = SessionLocal()
    stage_db = SessionLocal()
    task_log = None
    try:
        resume_repo = ResumeRepository(db)
        stage_repo = DocumentProcessingRepository(stage_db)
        task_log_repo = CeleryTaskLogRepository(db)

        task_log_service = CeleryTaskLogService(task_log_repo)
        stage_tracker = StageExecutionService(stage_repo)

        task_log = task_log_service.create_log(
            task_id=task_id,
            task_type="RESUME_DOCUMENT_PROCESSING",
        )

        pipeline = ResumeProcessingPipeline(
            preprocessing_service=PreprocessingService(),
            extraction_service=GeminiResumeExtractionService(),
            storage_service=StorageService(),
            resume_repository=resume_repo,
            stage_tracker=stage_tracker,
        )

        pipeline.run(
            task_id=task_id,
            resume_id=UUID(resume_id),
            candidate_id=UUID(candidate_id),
            file_path=file_path,
            file_format=FileFormat(file_format),
        )

        task_log.resume_id = UUID(resume_id)
        task_log_repo.update(task_log)
        task_log_repo.commit()
        task_log_service.mark_success(task_log, summary=f"Resume {resume_id} parsed.")

    except Exception as ex:
        if task_log:
            task_log_service.mark_failure(task_log, str(ex))
        logger.exception("Resume document processing task failed for task_id %s", task_id)
        raise

    finally:
        db.close()
        stage_db.close()
