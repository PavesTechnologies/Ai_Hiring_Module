import json
import logging

from sqlalchemy import text

from app.core.celery_app import celery_app
from app.db.session import SessionLocal
from app.models.async_tasks import TaskStatus
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.resume_repository import RESUME_EMBEDDINGS_IVFFLAT_INDEX, ResumeRepository
from app.services.celery_task_log_service import CeleryTaskLogService

logger = logging.getLogger(__name__)

REINDEX_IVFFLAT_TASK_TYPE = "REINDEX_IVFFLAT"


@celery_app.task(name="embedding.reindex_ivfflat_resume_embeddings", bind=True)
def reindex_ivfflat_resume_embeddings(self) -> None:
    """
    Embedding Storage Dashboard: rebuilds idx_resume_embeddings_embedding
    (ivfflat) once resume_embeddings has grown past
    EMBEDDING_REINDEX_THRESHOLD - ivfflat's clustering quality degrades as
    a table grows well past the row count it was built/tuned for, and a
    REINDEX rebuilds it against the table's current size. Plain `REINDEX
    INDEX` (not CONCURRENTLY - pgvector/Postgres versions here may not
    support concurrent reindex on all setups, and this is a maintenance
    task expected to run during low-traffic windows, not a live-traffic
    constraint) - takes a lock for its duration but resume_embeddings
    reads/writes elsewhere are otherwise unaffected once it completes.

    Never dispatched twice concurrently - EmbeddingDashboardService checks
    for an existing QUEUED/RUNNING REINDEX_IVFFLAT celery_task_log row
    before enqueueing this at all.
    """
    db = SessionLocal()
    task_log = None
    try:
        resume_repo = ResumeRepository(db)
        task_log_repo = CeleryTaskLogRepository(db)
        task_log_service = CeleryTaskLogService(task_log_repo)

        existing_task_log = task_log_repo.get_by_task_id(self.request.id)
        # Same broker-redelivery guard as every other resilient task in
        # this codebase - a REINDEX that already completed must never run
        # twice for the same task_id.
        if existing_task_log is not None and existing_task_log.status == TaskStatus.SUCCESS:
            logger.info(
                "REINDEX_IVFFLAT already completed for task_id=%s - skipping duplicate run.", self.request.id,
            )
            return

        if existing_task_log is None:
            existing_task_log = task_log_service.create_log(
                task_id=self.request.id,
                task_type=REINDEX_IVFFLAT_TASK_TYPE,
            )
        task_log = task_log_service.mark_running(existing_task_log)

        db.execute(text(f"REINDEX INDEX {RESUME_EMBEDDINGS_IVFFLAT_INDEX}"))
        db.commit()

        index_health = resume_repo.get_ivfflat_index_health()
        summary = json.dumps({"reindexed": True, "index_health": index_health})
        task_log_service.mark_success(task_log, summary=summary)
        logger.info("REINDEX_IVFFLAT completed | index_health=%s", index_health)

    except Exception as ex:
        db.rollback()
        if task_log:
            task_log_service.mark_failure(task_log, str(ex))
        logger.exception("REINDEX_IVFFLAT failed")

    finally:
        db.close()
