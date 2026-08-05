import json
import logging
from datetime import datetime, timezone
from uuid import UUID

from app.core.celery_app import celery_app
from app.db.session import SessionLocal
from app.enums.constants import ActionType, EntityType
from app.models.async_tasks import FailureClassification, TaskStatus
from app.repositories.audit_repository import AuditRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.dead_letter_queue_repository import DeadLetterQueueRepository
from app.repositories.jd_repository import JDRepository
from app.repositories.skill_repository import SkillRepository
from app.services.ai.embedding_service import EmbeddingService
from app.services.audit_service import AuditService
from app.services.celery_task_log_service import CeleryTaskLogService
from app.services.document_processing.error_classifier import classify
from app.services.document_processing.retry_policy import RetryPolicy, compute_backoff_seconds
from app.services.jd.jd_embedding_service import JDEmbeddingService

logger = logging.getLogger(__name__)

EMBED_JD_TASK_TYPE = "EMBED_JD"


_EMBED_JD_RETRY_POLICY = RetryPolicy(max_attempts=3, base_delay_seconds=10, max_delay_seconds=120)

_JD_EMBEDDING_UPDATED_CONDITION = "JD_EMBEDDING_UPDATED"


def _dead_letter_and_mark_dead(
    db, task_id, jd_id, jd_uuid, force_regenerate, attempt_number, task_log, task_log_service, error_message,
) -> None:
    try:
        DeadLetterQueueRepository(db).create(
            original_task_id=task_id,
            task_type=EMBED_JD_TASK_TYPE,
            final_error_message=error_message,
            full_error_trace=None,
            
            input_payload={"jd_id": jd_id, "force_regenerate": force_regenerate},
            retry_count=attempt_number,
            first_attempted_at=task_log.queued_at if task_log else datetime.now(timezone.utc),
            last_attempted_at=datetime.now(timezone.utc),
        )
        DeadLetterQueueRepository(db).commit()
    except Exception:
        logger.exception("Failed to dead-letter EMBED_JD for jd_id=%s", jd_id)
        db.rollback()

    if task_log:
        task_log_service.mark_dead(task_log, error_message)


def _raise_jd_embedding_updated_alerts(db, jd_repo: JDRepository, audit_service: AuditService, jd_id: UUID) -> None:
   
    linked_campaigns = jd_repo.get_linked_campaigns(jd_id)
    for campaign in linked_campaigns:
        audit_service.log(
            actor_id=None,
            actor_role="SYSTEM",
            action_type=ActionType.CAMPAIGN_HEALTH_ALERT,
            entity_type=EntityType.CAMPAIGN,
            entity_id=campaign.id,
            campaign_id=campaign.id,
            details={
                "title": f"JD embedding updated for campaign '{campaign.name}'",
                "condition": _JD_EMBEDDING_UPDATED_CONDITION,
                "jd_id": str(jd_id),
                "warning": (
                    "This campaign's job description embedding changed after a skill "
                    "update - candidates already semantically scored were scored against "
                    "the previous embedding. Re-run semantic scoring to reflect the update."
                ),
            },
        )
    db.commit()
    if linked_campaigns:
        logger.info(
            "Raised JD_EMBEDDING_UPDATED alerts | jd_id=%s campaigns_flagged=%s",
            jd_id, len(linked_campaigns),
        )


@celery_app.task(name="embedding.generate_jd_embedding", bind=True)
def generate_jd_embedding(self, task_id: str, jd_id: str, force_regenerate: bool = False) -> None:
    
    db = SessionLocal()
    task_log = None
    attempt_number = self.request.retries + 1
    jd_uuid = UUID(jd_id)
    try:
        jd_repo = JDRepository(db)
        skill_repo = SkillRepository(db)
        config_repo = ConfigRepository(db)
        audit_service = AuditService(AuditRepository(db))
        task_log_repo = CeleryTaskLogRepository(db)
        task_log_service = CeleryTaskLogService(task_log_repo)

        existing_task_log = task_log_repo.get_by_task_id(task_id)
        # Same broker-redelivery guard as generate_resume_embedding_task -
        # only a completed (SUCCESS) run short-circuits.
        if existing_task_log is not None and existing_task_log.status == TaskStatus.SUCCESS:
            logger.info(
                "JD embedding generation already completed for task_id=%s jd_id=%s - skipping duplicate run.",
                task_id, jd_id,
            )
            return

        job_description = jd_repo.get_by_id(jd_uuid)

        if existing_task_log is None:
            existing_task_log = task_log_service.create_log(
                task_id=task_id,
                task_type=EMBED_JD_TASK_TYPE,
                jd_id=job_description.id if job_description is not None else None,
            )
        task_log = task_log_service.mark_running(existing_task_log)

        logger.info(
            "EMBED_JD task started | jd_id=%s task_id=%s force_regenerate=%s",
            jd_id, task_id, force_regenerate,
        )

        if job_description is None:
            summary = json.dumps({"skipped": True, "reason": f"JobDescription {jd_id} no longer exists."})
            task_log_service.mark_success(task_log, summary=summary)
            logger.warning("EMBED_JD skipped | jd_id=%s reason=jd_deleted", jd_id)
            return

        if not job_description.is_active_version:
            summary = json.dumps({"skipped": True, "reason": "JD is not the active version."})
            task_log_service.mark_success(task_log, summary=summary)
            logger.info("EMBED_JD skipped | jd_id=%s reason=jd_not_active_version", jd_id)
            return

        embedding_service = JDEmbeddingService(jd_repo, skill_repo, config_repo, EmbeddingService())
        jd_embedding = embedding_service.generate_and_store_embedding(jd_uuid, force_regenerate=force_regenerate)
        db.commit()

        if force_regenerate:
            _raise_jd_embedding_updated_alerts(db, jd_repo, audit_service, jd_uuid)

        summary_payload = {
            "jd_id": str(jd_uuid),
            "jd_embedding_id": str(jd_embedding.id),
            "force_regenerate": force_regenerate,
        }
        task_log_service.mark_success(task_log, summary=json.dumps(summary_payload))
        logger.info(
            "EMBED_JD task completed | jd_id=%s jd_embedding_id=%s force_regenerate=%s",
            jd_id, jd_embedding.id, force_regenerate,
        )

    except Exception as ex:
        db.rollback()
        classification = classify(ex)

        if classification != FailureClassification.PERMANENT and attempt_number < _EMBED_JD_RETRY_POLICY.max_attempts:
            if task_log:
                task_log_service.mark_retry(task_log)
            delay = compute_backoff_seconds(_EMBED_JD_RETRY_POLICY, attempt_number)
            logger.warning(
                "EMBED_JD transient failure, retrying | jd_id=%s attempt=%s delay=%ss error=%s",
                jd_id, attempt_number, delay, ex,
            )
            self.retry(exc=ex, countdown=delay, max_retries=_EMBED_JD_RETRY_POLICY.max_attempts)
            return

        error_message = str(ex)
        _dead_letter_and_mark_dead(
            db, task_id, jd_id, jd_uuid, force_regenerate, attempt_number, task_log, task_log_service, error_message,
        )
        logger.info("EMBED_JD task failed | jd_id=%s task_id=%s", jd_id, task_id)
        logger.exception("EMBED_JD task permanently failed for jd_id %s", jd_id)

    finally:
        db.close()
