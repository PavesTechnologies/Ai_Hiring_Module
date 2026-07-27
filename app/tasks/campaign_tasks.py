from app.core.celery_app import celery_app
from app.db.session import SessionLocal

from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.audit_repository import AuditRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository

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