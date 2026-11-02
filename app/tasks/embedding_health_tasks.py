import json
import logging
from uuid import uuid4

from app.core.celery_app import celery_app
from app.core.config import settings
from app.db.session import SessionLocal
from app.enums.constants import ActionType, EntityType
from app.models.campaigns import CampaignStatus
from app.models.config import CBState
from app.models.email import EmailTriggerEvent
from app.models.identity import UserRole
from app.repositories.audit_repository import AuditRepository
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.circuit_breaker_repository import CircuitBreakerRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.user_repository import UserRepository
from app.services.audit_service import AuditService
from app.services.celery_task_log_service import CeleryTaskLogService
from app.services.notifications.internal_user_emails import queue_internal_user_emails
from app.tasks.embedding_tasks import EMBEDDING_SERVICE_NAME

logger = logging.getLogger(__name__)

EMBEDDING_HEALTH_CHECK_TASK_TYPE = "EMBEDDING_HEALTH_CHECK"
_EMBEDDING_FAILURE_ALERT_THRESHOLD_KEY = "EMBEDDING_FAILURE_ALERT_THRESHOLD"
_DEFAULT_EMBEDDING_FAILURE_ALERT_THRESHOLD = 20.0
_EMBEDDING_FAILURE_RATE_EXCEEDED_CONDITION = "EMBEDDING_FAILURE_RATE_EXCEEDED"


def _read_alert_threshold(config_repo: ConfigRepository) -> float:
    raw = config_repo.get_configs_by_keys([_EMBEDDING_FAILURE_ALERT_THRESHOLD_KEY]).get(
        _EMBEDDING_FAILURE_ALERT_THRESHOLD_KEY,
    )
    if raw is None:
        return _DEFAULT_EMBEDDING_FAILURE_ALERT_THRESHOLD
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid EMBEDDING_FAILURE_ALERT_THRESHOLD platform_config value %r - falling back to default %s.",
            raw, _DEFAULT_EMBEDDING_FAILURE_ALERT_THRESHOLD,
        )
        return _DEFAULT_EMBEDDING_FAILURE_ALERT_THRESHOLD


def _build_campaign_monitoring_link(campaign_id) -> str:
    base = settings.frontend_base_url.rstrip("/") if settings.frontend_base_url else ""
    return f"{base}/campaigns/{campaign_id}"


def _send_embedding_health_alert(
    db, user_repo: UserRepository, campaign, affected_count: int, total_screening_count: int,
    failure_pct: float, cb_state,
) -> int:
    """
    Requirement 5's "send HR_ADMIN email": one EMBEDDING_HEALTH_ALERT notification per active
    HR_ADMIN, through the notifications path like every other email (recorded in
    email_notifications, governed by EMAIL_DELIVERY, opt-outs honoured). Returns how many were queued.
    """
    hr_admins = user_repo.get_active_by_role(UserRole.HR_ADMIN)
    if not hr_admins:
        logger.warning(
            "No active HR_ADMIN users to notify for embedding health alert on campaign_id=%s", campaign.id,
        )
        return 0

    suspended = cb_state is not None and cb_state.state == CBState.OPEN
    return queue_internal_user_emails(db, hr_admins, EmailTriggerEvent.EMBEDDING_HEALTH_ALERT, {
        "campaign_name": campaign.name,
        "affected_count": affected_count,
        "total_screening_count": total_screening_count,
        "failure_percentage": f"{failure_pct:.2f}",
        "circuit_breaker_state": cb_state.state.value if cb_state is not None else CBState.CLOSED.value,
        "monitoring_link": _build_campaign_monitoring_link(campaign.id),
        "suspended_note": ("Embedding generation is currently SUSPENDED (circuit breaker OPEN) - "
                           "new resumes will not be embedded until it recovers.") if suspended else "",
    })


@celery_app.task(name="embedding.monitor_health")
def monitor_embedding_health() -> None:
    """
    Requirement 5: runs every 30 minutes (celery_app.py's beat_schedule).
    For each ACTIVE campaign, calculates the percentage of SCREENING
    candidates with a NULL semantic_score that haven't already been
    triaged to MANUAL_REVIEW (see
    CampaignCandidateRepository.get_screening_semantic_health_stats) - if
    it exceeds EMBEDDING_FAILURE_ALERT_THRESHOLD, queues an
    EMBEDDING_HEALTH_ALERT email to every active HR_ADMIN through the
    notifications path (see _send_embedding_health_alert) and records the
    alert via ActionType.PLATFORM_ALERT_SENT, with how many were queued.
    """
    db = SessionLocal()
    task_log = None
    try:
        campaign_repo = CampaignRepository(db)
        campaign_candidate_repo = CampaignCandidateRepository(db)
        config_repo = ConfigRepository(db)
        circuit_breaker_repo = CircuitBreakerRepository(db)
        user_repo = UserRepository(db)
        audit_service = AuditService(AuditRepository(db))
        task_log_repo = CeleryTaskLogRepository(db)
        task_log_service = CeleryTaskLogService(task_log_repo)

        task_log = task_log_service.create_log(
            task_id=str(uuid4()),
            task_type=EMBEDDING_HEALTH_CHECK_TASK_TYPE,
        )

        threshold = _read_alert_threshold(config_repo)
        cb_state = circuit_breaker_repo.get_by_service_name(EMBEDDING_SERVICE_NAME)

        active_campaigns = [
            campaign for campaign in campaign_repo.get_all_campaigns(show_closed=False)
            if campaign.status == CampaignStatus.ACTIVE
        ]

        alerts_raised = 0
        for campaign in active_campaigns:
            affected_count, total_screening_count = campaign_candidate_repo.get_screening_semantic_health_stats(
                campaign.id,
            )
            if total_screening_count == 0:
                continue

            failure_pct = (affected_count / total_screening_count) * 100
            if failure_pct <= threshold:
                continue

            emails_queued = _send_embedding_health_alert(
                db, user_repo, campaign, affected_count, total_screening_count, failure_pct, cb_state,
            )

            audit_service.log(
                actor_id=None,
                actor_role="SYSTEM",
                action_type=ActionType.PLATFORM_ALERT_SENT,
                entity_type=EntityType.CAMPAIGN,
                entity_id=campaign.id,
                campaign_id=campaign.id,
                details={
                    "condition": _EMBEDDING_FAILURE_RATE_EXCEEDED_CONDITION,
                    "campaign_name": campaign.name,
                    "affected_count": affected_count,
                    "total_screening_count": total_screening_count,
                    "failure_percentage": round(failure_pct, 2),
                    "threshold": threshold,
                    "circuit_breaker_state": cb_state.state.value if cb_state is not None else CBState.CLOSED.value,
                    "emails_queued": emails_queued,
                },
            )
            # Each alert's audit entry on its own: a failed email queue for a later campaign
            # rolls the session back and must not take this one with it.
            db.commit()
            alerts_raised += 1

        db.commit()
        summary = json.dumps({"campaigns_checked": len(active_campaigns), "alerts_raised": alerts_raised})
        task_log_service.mark_success(task_log, summary=summary)
        logger.info(
            "Embedding health check completed | campaigns_checked=%s alerts_raised=%s",
            len(active_campaigns), alerts_raised,
        )

    except Exception as ex:
        db.rollback()
        if task_log:
            task_log_service.mark_failure(task_log, str(ex))
        logger.exception("Embedding health check failed")

    finally:
        db.close()
