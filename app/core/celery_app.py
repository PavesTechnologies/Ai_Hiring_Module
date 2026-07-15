import platform

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

# ── Worker pool: Windows dev vs. Linux production ────────────────────────────
# billiard's prefork pool (Celery's default) forks worker processes with
# os.fork(), which Windows does not support — it surfaces as a PermissionError
# at worker startup rather than a clean "unsupported" error. Detecting the
# platform here (instead of hardcoding a pool in a startup script/CLI flag)
# means the same `celery -A app.core.celery_app worker` command works
# unchanged on every developer's Windows machine AND in production, with no
# env var or manual flag to remember. Linux production hosts fall through
# untouched and keep Celery's default "prefork" pool (multi-process, so it
# still scales across CPU cores there).
if platform.system() == "Windows":
    celery_app.conf.worker_pool = "solo"

celery_app.conf.imports = (
    "app.tasks.campaign_tasks",
    "app.tasks.jd_processing_tasks",
    "app.tasks.skill_ontology_tasks",
)

celery_app.conf.beat_schedule = {
    "auto-close-expired-campaigns": {
        "task": "campaign.auto_close_expired_campaigns",
        "schedule": crontab( minute=0, hour=0 ),
    },
    "detect-duplicate-skill-aliases": {
        "task": "skill.detect_duplicate_aliases",
        # Nightly, off-peak — a full skill_ontology scan, not time-sensitive.
        "schedule": crontab(minute=0, hour=2),
    },
}