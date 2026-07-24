from app.core.celery_app import celery_app
from app.db.session import SessionLocal

from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.audit_repository import AuditRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.config_repository import ConfigRepository

from app.services.audit_service import AuditService
from app.services.campaign.campaign_scheduler_service import (
    CampaignSchedulerService,
)
from app.services.celery_task_log_service import CeleryTaskLogService

@celery_app.task(name="campaign.auto_close_expired_campaigns")

def auto_close_expired_campaigns():
    """
    Background task to automatically close expired campaigns.
    """

    db = SessionLocal()

    task_log = None
    print("########## TASK STARTED ##########")
    try:
        campaign_repo = CampaignRepository(db)
        audit_repo = AuditRepository(db)
        task_log_repo = CeleryTaskLogRepository(db)
        config_repo = ConfigRepository(db)

        audit_service = AuditService(audit_repo)
        task_log_service = CeleryTaskLogService(task_log_repo)

        # Create task log
        task_log = task_log_service.create_log(
            task_id=auto_close_expired_campaigns.request.id,
            task_type="CAMPAIGN_AUTO_CLOSE",
        )

        scheduler_service = CampaignSchedulerService(
            campaign_repo=campaign_repo,
            audit_service=audit_service,
            config_repo=config_repo,
        )

        closed = scheduler_service.auto_close_expired_campaigns()

        # Mark success
        task_log_service.mark_success(
            task_log,
            summary=(
                "No expired campaigns found."
                if closed == 0
                else f"Closed {closed} expired campaigns."
            ),
        )

        print(f"Closed {closed} expired campaigns.")
        return closed

    except Exception as ex:

        if task_log:
            task_log_service.mark_failure(
                task_log,
                str(ex),
            )

        print(f"Celery Task Failed : {ex}")
        raise

    finally:
        db.close()


@celery_app.task(name="campaign.evaluate_health_alerts")
def evaluate_campaign_health_alerts():
    """
    daily background task evaluating pipeline health for every
    ACTIVE campaign (DEAD task count, deterministic rejection rate,
    average SCREENING time, FRAUD_REVIEW count) against platform_config
    thresholds, raising a CAMPAIGN_HEALTH_ALERT audit entry per condition
    triggered.
    """

    db = SessionLocal()

    task_log = None
    try:
        campaign_repo = CampaignRepository(db)
        audit_repo = AuditRepository(db)
        task_log_repo = CeleryTaskLogRepository(db)
        config_repo = ConfigRepository(db)

        audit_service = AuditService(audit_repo)
        task_log_service = CeleryTaskLogService(task_log_repo)

        task_log = task_log_service.create_log(
            task_id=evaluate_campaign_health_alerts.request.id,
            task_type="CAMPAIGN_HEALTH_ALERT_CHECK",
        )

        scheduler_service = CampaignSchedulerService(
            campaign_repo=campaign_repo,
            audit_service=audit_service,
            config_repo=config_repo,
        )

        alerts_raised = scheduler_service.evaluate_campaign_health_alerts()

        task_log_service.mark_success(
            task_log,
            summary=(
                "No campaign health alerts triggered."
                if alerts_raised == 0
                else f"Raised {alerts_raised} campaign health alert(s)."
            ),
        )

        return alerts_raised

    except Exception as ex:

        if task_log:
            task_log_service.mark_failure(
                task_log,
                str(ex),
            )

        raise

    finally:
        db.close()