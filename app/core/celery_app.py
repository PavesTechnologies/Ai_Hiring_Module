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
    "app.tasks.resume_processing_tasks",
    "app.tasks.bulk_upload_tasks",
    "app.tasks.skill_ontology_tasks",
    "app.tasks.deterministic_scoring_tasks",
)


def _get_deadline_check_interval_hours() -> int:
    """
    E03-S05-T02: sourced from platform_config.DEADLINE_CHECK_INTERVAL_HOURS —
    read once here at Celery process startup, since beat_schedule is a static
    dict evaluated at import time, not a live per-tick DB read. Changing the
    config value later requires restarting the beat process to take effect.
    Falls back to hourly if the DB isn't reachable yet at import time.
    """
    try:
        from app.db.session import SessionLocal
        from app.repositories.config_repository import ConfigRepository

        db = SessionLocal()
        try:
            value = ConfigRepository(db).get_configs_by_keys(
                ["DEADLINE_CHECK_INTERVAL_HOURS"]
            ).get("DEADLINE_CHECK_INTERVAL_HOURS")
            return int(value) if value else 1
        finally:
            db.close()
    except Exception:
        return 1


_deadline_check_interval_hours = _get_deadline_check_interval_hours()

celery_app.conf.beat_schedule = {
    "auto-close-expired-campaigns": {
        "task": "campaign.auto_close_expired_campaigns",
        "schedule": crontab(minute=0, hour=f"*/{_deadline_check_interval_hours}"),
    },
    "detect-duplicate-skill-aliases": {
        "task": "skill.detect_duplicate_aliases",
        # Nightly, off-peak — a full skill_ontology scan, not time-sensitive.
        "schedule": crontab(minute=0, hour=2),
    },
    "evaluate-campaign-health-alerts": {
        "task": "campaign.evaluate_health_alerts",
        # spec says "daily" with no config-sourced interval, so
        # this one is a plain fixed off-peak hour, distinct from the other
        # two daily/hourly jobs.
        "schedule": crontab(minute=0, hour=3),
    },
}