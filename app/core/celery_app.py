from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery(
    "airs",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

celery_app.conf.update(
    timezone="UTC",
    enable_utc=True,
)

celery_app.conf.imports = (
    "app.tasks.campaign_tasks",
    "app.tasks.jd_processing_tasks",
    "app.tasks.resume_processing_tasks",
    "app.tasks.bulk_upload_tasks",
)

celery_app.conf.beat_schedule = {
    "auto-close-expired-campaigns": {
        "task": "campaign.auto_close_expired_campaigns",
        "schedule": crontab( minute=0, hour=0 ),
    },
}