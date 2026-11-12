"""Golden-set runs for a scoring version, started from the Settings page (app/services/settings/golden_runs.py)."""
import logging

from app.core.celery_app import celery_app
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)


@celery_app.task(name="settings.golden_run")
def golden_run_task(run_id: str) -> None:
    from app.services.settings.golden_runs import execute
    db = SessionLocal()
    try:
        execute(db, run_id)
    except Exception:
        logger.exception("Golden run failed | run_id=%s", run_id)
    finally:
        db.close()
