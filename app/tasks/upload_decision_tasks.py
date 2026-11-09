import json
import logging
from uuid import uuid4

from app.core.celery_app import celery_app
from app.core.single_run import single_run
from app.db.session import SessionLocal
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.services.celery_task_log_service import CeleryTaskLogService
from app.services.resume.upload_decision_service import UploadDecisionService

logger = logging.getLogger(__name__)

UPLOAD_DECISION_EXPIRY_TASK_TYPE = "UPLOAD_DECISION_EXPIRY"


@celery_app.task(name="resume.expire_upload_decisions")
@single_run("resume.expire_upload_decisions")
def expire_upload_decisions_task() -> None:
    """
    Celery Beat, daily: every bulk-upload decision still PENDING 30 days after
    it was raised keeps the existing resume - the held file is deleted, the
    decision becomes EXPIRED_KEPT_EXISTING, audited as SYSTEM.
    """
    db = SessionLocal()
    task_log = None
    try:
        task_log_service = CeleryTaskLogService(CeleryTaskLogRepository(db))
        task_log = task_log_service.create_log(task_id=str(uuid4()), task_type=UPLOAD_DECISION_EXPIRY_TASK_TYPE)

        expired = UploadDecisionService(db).expire_pending()

        task_log_service.mark_success(task_log, summary=json.dumps({"expired": expired}))
        logger.info("Upload decision expiry completed | expired=%s", expired)
    except Exception as ex:
        db.rollback()
        if task_log:
            task_log_service.mark_failure(task_log, str(ex))
        logger.exception("Upload decision expiry failed")
    finally:
        db.close()
