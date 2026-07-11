import logging

from app.core.celery_app import celery_app
from app.db.session import SessionLocal

from app.repositories.audit_repository import AuditRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.document_processing_repository import DocumentProcessingRepository
from app.repositories.jd_repository import JDRepository
from app.repositories.skill_repository import SkillRepository

from app.services.ai.embedding_service import EmbeddingService
from app.services.ai.preprocessing_service import PreprocessingService
from app.services.audit_service import AuditService
from app.services.celery_task_log_service import CeleryTaskLogService
from app.services.document_processing.stage_execution_service import StageExecutionService
from app.services.extractions.gemini_extraction_service import GeminiExtractionService
from app.services.jd.hash_service import HashService
from app.services.jd.jd_processing_pipeline import JDProcessingPipeline
from app.services.jd.jd_service import JDService
from app.services.skills.skill_normalization_service import SkillNormalizationService
from app.core.storage_service import StorageService

logger = logging.getLogger(__name__)


@celery_app.task(name="jd.process_document")
def process_jd_document(
    task_id: str,
    raw_text: str | None,
    file_path: str | None,
    title: str,
    jurisdiction: str,
    min_experience_years: float | None,
    education_criteria: dict | None,
    created_by: str,
) -> None:
    """
    Background leg of the JD processing pipeline (everything after
    Validation/Storage, which already ran synchronously in the route):
    Text Extraction -> Text Cleaning -> AI Extraction -> JSON Validation ->
    Skill Normalization -> Embedding Generation -> Persistence.

    Stage tracking runs on its own session (`stage_db`), separate from the
    business-write session (`db`). StageExecutionService commits once at
    the start and once at the end of every stage — if it shared a session
    with JDRepository/SkillRepository/AuditRepository, those frequent
    commits would finalize whatever business writes happened to be pending
    at that moment, undermining the "nothing persists before Persistence
    succeeds" guarantee. Keeping them on separate connections makes that
    guarantee structural rather than incidental (today it only holds
    because no pre-Persistence stage happens to write business data).
    """
    db = SessionLocal()
    stage_db = SessionLocal()
    task_log = None
    try:
        jd_repo = JDRepository(db)
        skill_repo = SkillRepository(db)
        stage_repo = DocumentProcessingRepository(stage_db)
        audit_repo = AuditRepository(db)
        task_log_repo = CeleryTaskLogRepository(db)

        audit_service = AuditService(audit_repo)
        task_log_service = CeleryTaskLogService(task_log_repo)
        stage_tracker = StageExecutionService(stage_repo)

        task_log = task_log_service.create_log(
            task_id=task_id,
            task_type="JD_DOCUMENT_PROCESSING",
        )

        jd_service = JDService(
            repository=jd_repo,
            hash_service=HashService(),
            audit_service=audit_service,
            storage_service=StorageService(),
        )

        pipeline = JDProcessingPipeline(
            preprocessing_service=PreprocessingService(),
            extraction_service=GeminiExtractionService(),
            hash_service=HashService(),
            storage_service=StorageService(),
            skill_normalization_service=SkillNormalizationService(skill_repo),
            embedding_service=EmbeddingService(),
            jd_service=jd_service,
            jd_repository=jd_repo,
            skill_repository=skill_repo,
            stage_tracker=stage_tracker,
        )

        jd_id = pipeline.run(
            task_id=task_id,
            raw_text=raw_text,
            file_path=file_path,
            title=title,
            jurisdiction=jurisdiction,
            min_experience_years=min_experience_years,
            education_criteria=education_criteria,
            created_by=created_by,
        )

        if jd_id:
            task_log.jd_id = jd_id
            task_log_repo.update(task_log)
            task_log_repo.commit()
            task_log_service.mark_success(task_log, summary=f"JD {jd_id} created.")
        else:
            task_log_service.mark_success(task_log, summary="Duplicate job description; no new JD created.")

    except Exception as ex:
        if task_log:
            task_log_service.mark_failure(task_log, str(ex))
        logger.exception("JD document processing task failed for task_id %s", task_id)
        raise

    finally:
        db.close()
        stage_db.close()
