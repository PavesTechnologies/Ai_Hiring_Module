import logging
from uuid import UUID

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
    max_experience_years: float | None = None,
    notice_period: int | None = None,
    existing_jd_id: str | None = None,
    version_number: int = 1,
    parent_jd_id: str | None = None,
    lineage_root_id: str | None = None,
    old_file_path: str | None = None,
) -> None:
    """
    Background leg of the JD processing pipeline (everything after
    Validation/Storage, which already ran synchronously in the route):
    Text Extraction -> Text Cleaning -> AI Extraction -> JSON Validation ->
    Skill Normalization -> Embedding Generation -> Persistence.

    existing_jd_id/version_number/parent_jd_id/lineage_root_id are only set
    for an update-triggered reprocess (JDService.update_jd() returned
    JDReprocessRequired) — absent, this is a normal create run. old_file_path
    is the document a reprocess is replacing, deleted only after the new
    version has successfully persisted (mirrors the cleanup update_jd()
    used to do synchronously, moved here since persistence itself is now
    async for this path too).

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

        # One EmbeddingService instance shared by the pipeline's own JD-level
        # embedding stage and by skill-level semantic matching — the
        # underlying sentence-transformer model is a class-level singleton
        # either way, but there's no reason to instantiate the wrapper twice.
        embedding_service = EmbeddingService()

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
        )

        jd_id = pipeline.run(
            task_id=task_id,
            raw_text=raw_text,
            file_path=file_path,
            title=title,
            jurisdiction=jurisdiction,
            min_experience_years=min_experience_years,
            max_experience_years=max_experience_years,
            notice_period=notice_period,
            education_criteria=education_criteria,
            created_by=created_by,
            existing_jd_id=UUID(existing_jd_id) if existing_jd_id else None,
            version_number=version_number,
            parent_jd_id=UUID(parent_jd_id) if parent_jd_id else None,
            lineage_root_id=UUID(lineage_root_id) if lineage_root_id else None,
        )

        if jd_id:
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
        else:
            task_log_service.mark_success(task_log, summary="Duplicate job description; no new JD created.")

    except Exception as ex:
        # A DB-level failure inside pipeline.run() leaves `db`'s transaction
        # aborted; mark_failure reuses the same session, so without this
        # rollback its own read/write dies with InFailedSqlTransaction and
        # masks the real error above.
        db.rollback()
        if task_log:
            task_log_service.mark_failure(task_log, str(ex))
        logger.exception("JD document processing task failed for task_id %s", task_id)
        raise

    finally:
        db.close()
        stage_db.close()
