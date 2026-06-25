from celery import Celery

from app.core.config import settings

celery = Celery(
    "airs",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "app.workers.resume_worker",
        "app.workers.embedding_worker",
        "app.workers.ai_eval_worker",
        "app.workers.retention_worker",
    ],
)

celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)
