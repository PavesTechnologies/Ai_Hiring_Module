import logging
import logging.handlers
import os
import platform
from pathlib import Path

from celery import Celery, signals
from celery.schedules import crontab
from kombu import Queue

from app.core.config import settings

# Review item 20. Redis has no real ack: a message a worker takes - to run it, to hold it for a retry
# countdown, or reserved behind another task - returns to the queue once VISIBILITY_TIMEOUT_SECONDS
# pass, and a second worker runs it too. So the time a worker can hold a message (the longest retry
# countdown plus the longest run) must stay under the visibility timeout:
#   MAX_RETRY_COUNTDOWN_SECONDS + TASK_TIME_LIMIT_SECONDS < VISIBILITY_TIMEOUT_SECONDS
# (tests/core/test_task_timing.py checks it). The timeout was 300 s, under the 480 s retry countdowns
# of the decision-pending and v2 policies; it is back at Celery's default. A worker killed outright
# (no warm shutdown, which returns its unacked messages at once) now leaves its tasks parked for up
# to an hour; the recovery sweeps' age limits are tied to this value so they never race the broker.
VISIBILITY_TIMEOUT_SECONDS = 3600
MAX_RETRY_COUNTDOWN_SECONDS = 1800
TASK_SOFT_TIME_LIMIT_SECONDS = 1200     # SoftTimeLimitExceeded inside the task: its normal failure path
TASK_TIME_LIMIT_SECONDS = 1500          # the worker process is killed


def bounded_countdown(seconds: float) -> float:
    """A retry countdown capped so the held message can never outlive the visibility timeout."""
    return max(0.0, min(float(seconds), float(MAX_RETRY_COUNTDOWN_SECONDS)))


celery_app = Celery(
    "airs",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

celery_app.conf.update(
    timezone="UTC",
    enable_utc=True,
    # Ops fix: by default Celery acks (removes) a message the instant a
    # worker *receives* it, before the task body runs — if that worker
    # process then dies (crash, manual restart, deploy) before actually
    # executing, the message is gone forever with no redelivery, leaving
    # its celery_task_log row stuck at QUEUED/RETRY permanently (observed
    # live: 3 resume-processing tasks orphaned this way after a worker
    # restart). task_acks_late defers the ack until after the task
    # finishes (success or failure); task_reject_on_worker_lost requeues
    # the message instead of silently dropping it if the worker is killed
    # mid-execution. This makes every task at-least-once rather than
    # at-most-once — safe here since resume/bulk processing is already
    # idempotent by design (duplicate-file/candidate detection, version
    # bumping), so a redelivered/re-run task cannot double-create data.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    broker_connection_retry_on_startup=True,
    # 3 here limits callers that only publish (the API). The worker itself
    # retries forever - see _worker_keeps_retrying_the_broker below.
    broker_connection_max_retries=3,
    # Redis has no real server-side ack: a task handed to a worker is moved
    # to an "unacked" set and only returned to the queue once
    # visibility_timeout elapses. Celery's default is 3600s, so a worker
    # killed mid-task (a dev auto-reload, a deploy, a crash) leaves that
    # task invisible for a full hour — celery_task_log says RUNNING, the
    # queue reads empty, the worker sits idle, and it looks permanently
    # stuck. Observed live: 4 resume tasks parked this way for 25-37
    # minutes with no way to tell they were still pending.
    #
    # It was lowered to 300s for that faster recovery, but the clock also
    # runs while a worker holds a retry countdown or a reserved task, and the
    # decision-pending and v2 retries wait up to 480s - those ran twice
    # (review item 20). Back at the default, with hard time limits that keep
    # countdown + run under it; see VISIBILITY_TIMEOUT_SECONDS above. A warm
    # shutdown still returns unacked messages at once, and the recovery
    # sweeps (resume redispatch, stalled-task sweep) cover a hard kill.
    broker_transport_options={"visibility_timeout": VISIBILITY_TIMEOUT_SECONDS},
    task_soft_time_limit=TASK_SOFT_TIME_LIMIT_SECONDS,
    task_time_limit=TASK_TIME_LIMIT_SECONDS,
    # Nothing reads Celery results (task state lives in celery_task_log),
    # so don't write a celery-task-meta key per task into the shared Redis.
    task_ignore_result=True,
    result_expires=3600,
    worker_prefetch_multiplier=settings.celery_worker_prefetch_multiplier,
)

# ── Queues: LLM tasks isolated from everything else ─────────────────────────
# Only these tasks call the LLM provider. Routing them to their own queue lets
# a deployment run one worker for them at low concurrency (capping concurrent
# provider requests, so no 429 storms) and another for scoring/embeddings/
# emails at higher concurrency - neither waits behind the other. Routing is by
# task name, so every enqueue path (API, retries, DLQ replay, recovery scans)
# follows it automatically.
LLM_TASK_NAMES = (
    "jd.process_document",
    "resume.process_document",
    "bulk_upload.parse_file",
    "scoring.calculate_ai_evaluation",
)

_queue_names = list(dict.fromkeys([settings.celery_default_queue, settings.celery_llm_queue]))
celery_app.conf.task_default_queue = settings.celery_default_queue
# A worker started without -Q consumes every queue declared here, so a single
# worker (local dev, or a small deployment) still runs everything.
celery_app.conf.task_queues = tuple(Queue(name) for name in _queue_names)
celery_app.conf.task_routes = {name: {"queue": settings.celery_llm_queue} for name in LLM_TASK_NAMES}
if settings.celery_llm_task_rate_limit:
    celery_app.conf.task_annotations = {
        name: {"rate_limit": settings.celery_llm_task_rate_limit} for name in LLM_TASK_NAMES
    }

# ── Worker pool ──────────────────────────────────────────────────────────────
# billiard's prefork pool (Celery's default) forks worker processes with
# os.fork(), which Windows does not support — it surfaces as a PermissionError
# at worker startup rather than a clean "unsupported" error. So Windows falls
# back to "solo" (one task at a time) unless CELERY_WORKER_POOL overrides it -
# "threads" runs tasks in parallel there (fine for this I/O-bound workload;
# raise DB_POOL_SIZE with --concurrency, since threads share one engine).
# Linux keeps Celery's default "prefork" unless overridden.
if settings.celery_worker_pool:
    celery_app.conf.worker_pool = settings.celery_worker_pool
elif platform.system() == "Windows":
    celery_app.conf.worker_pool = "solo"

celery_app.conf.imports = (
    "app.tasks.campaign_tasks",
    "app.tasks.jd_processing_tasks",
    "app.tasks.embedding_tasks",
    "app.tasks.resume_processing_tasks",
    "app.tasks.bulk_upload_tasks",
    "app.tasks.skill_ontology_tasks",
    "app.tasks.deterministic_scoring_tasks",
    "app.tasks.semantic_scoring_tasks",
    "app.tasks.ai_evaluation_tasks",
    "app.tasks.scoring_v2_tasks",
    "app.tasks.full_scoring_tasks",
    "app.tasks.composite_scoring_tasks",
    "app.tasks.jd_embedding_tasks",
    "app.tasks.embedding_health_tasks",
    "app.tasks.reindex_tasks",
    "app.tasks.talent_pool_tasks",
    "app.tasks.interview_tasks",
    "app.tasks.export_tasks",
    "app.tasks.email_tasks",
    "app.tasks.email_health_tasks",
    "app.tasks.compliance_tasks",
    "app.tasks.unknown_skill_reevaluation_tasks",
    "app.tasks.upload_decision_tasks",
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
    "detect-stalled-candidates": {
        "task": "campaign.detect_stalled_candidates",
        # M04-E04-S04: daily, offset from the health-alert sweep so the two
        # campaign-wide scans never contend for the same rows.
        "schedule": crontab(minute=30, hour=3),
    },
    "evaluate-resubmission-alerts": {
        "task": "campaign.evaluate_resubmission_alerts",
        # Epic 3 (M05-E03) Phase C4 — daily, off-peak, its own distinct hour
        # from the other three scheduled jobs.
        "schedule": crontab(minute=0, hour=4),
    },
    "recover-pending-semantic-scores": {
        "task": "scoring.recover_pending_semantic_scores",
        # M08-E02 resilient-retry requirement 4 (automatic recovery) — no
        # specific interval given in the spec, so a frequent-but-cheap
        # every-15-minutes scan (the query itself is a single indexed join,
        # not a full-table scan) catches a resume's embedding becoming
        # available soon after it does, without needing a config key.
        "schedule": crontab(minute="*/15"),
    },
    "monitor-embedding-health": {
        "task": "embedding.monitor_health",
        # Requirement 5: runs every 30 minutes exactly, per spec.
        "schedule": crontab(minute="*/30"),
    },
    "monitor-stuck-email-sends": {
        "task": "notifications.monitor_stuck_sends",
        # Emails left SENDING over 30 minutes (outcome unknown, never re-sent automatically) -
        # HR_ADMIN alert, at most once per newly stuck email or every 24 h while still stuck.
        "schedule": crontab(minute="7-59/15"),
    },
    "validate-talent-pool-eligibility": {
        "task": "embedding.validate_talent_pool_eligibility",
        # Talent Pool Eligibility requirement: "daily" per spec, off-peak,
        # its own distinct hour from the other daily jobs above.
        "schedule": crontab(minute=0, hour=5),
    },
    "complete-full-scoring": {
        "task": "scoring.complete_full_scoring_scan",
        # No-LLM fallback HOLDs: full scoring once quota is back (the scan
        # itself stops queueing when the daily cap has no room left).
        "schedule": crontab(minute=40),
    },
    "recover-undecided-candidates": {
        "task": "scoring.recover_undecided_candidates",
        # A screening decision that never landed (v2 PENDING / NOT_RUN through
        # every retry, or scoring never queued) is queued again - hourly, each
        # candidate at most once per SCORING_DECISION_RECOVERY_HOURS.
        "schedule": crontab(minute=20),
    },
    "recover-stalled-resume-uploads": {
        "task": "resume.recover_stalled_uploads",
        # Resume-upload resilience: periodic safety net alongside the
        # FastAPI startup scan, in case the broker was still unreachable
        # when a given app instance started - frequent since a stuck
        # upload leaves a candidate's resume unprocessed for as long as
        # this interval.
        "schedule": crontab(minute="*/5"),
    },
    "retry-dead-lettered-erasure-file-deletes": {
        "task": "compliance.retry_dead_lettered_file_deletes",
        # Erased candidates' files whose delete kept failing: run each again once a day; a success
        # deletes the dead-letter entry. Its own off-peak hour.
        "schedule": crontab(minute=45, hour=5),
    },
    "expire-upload-decisions": {
        "task": "resume.expire_upload_decisions",
        # Duplicate resume decisions: a held bulk file nobody decided on in
        # 30 days keeps the existing resume. Daily, off-peak, its own hour.
        "schedule": crontab(minute=30, hour=4),
    },
    "request-interview-feedback-for-ended-rounds": {
        "task": "interview.request_feedback_for_ended_rounds",
        # Epic 5 Step 4 - a feedback-request email only makes sense once
        # a round's end_at has passed; hourly is the real middle ground
        # this codebase already uses (see auto-close-expired-campaigns) -
        # frequent enough that a request goes out within an hour of the
        # interview ending, cheap enough (a single indexed range query)
        # not to need a longer interval like the daily campaign-wide scans.
        "schedule": crontab(minute=0),
    },
}


# ── Worker: keep retrying the broker; log to a rotating file ────────────────

@signals.celeryd_init.connect
def _worker_keeps_retrying_the_broker(conf=None, **_):
    """
    In the worker only: retry the Redis connection forever instead of
    exiting after broker_connection_max_retries (3). A worker that gave up
    exited, and under watchmedo that looked like an unexplained restart.
    Publishers (the API) keep the limit, so a request never waits on Redis
    indefinitely.
    """
    (conf or celery_app.conf).broker_connection_max_retries = None


def _add_worker_log_file(logger: logging.Logger, format: str) -> None:
    path = settings.celery_worker_log_file
    if not path or any(getattr(h, "_airs_worker_log", False) for h in logger.handlers):
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=settings.celery_worker_log_max_bytes,
        backupCount=settings.celery_worker_log_backup_count, encoding="utf-8", delay=True,
    )
    handler.setFormatter(logging.Formatter(format))
    handler._airs_worker_log = True
    logger.addHandler(handler)


@signals.after_setup_logger.connect
def _worker_log_file(logger=None, format=None, **_):
    """Worker + app logs also go to CELERY_WORKER_LOG_FILE (rotating), when set."""
    _add_worker_log_file(logger, format or "[%(asctime)s: %(levelname)s/%(processName)s] %(message)s")


@signals.after_setup_task_logger.connect
def _worker_task_log_file(logger=None, format=None, **_):
    _add_worker_log_file(logger, format or "[%(asctime)s: %(levelname)s/%(processName)s] %(message)s")


_lifecycle_log = logging.getLogger("airs.worker")


@signals.worker_ready.connect
def _log_worker_started(**_):
    # A restart with no "stopping" line before it means the process was
    # killed (e.g. watchmedo after a .py change), not that it exited itself.
    _lifecycle_log.warning("AIRS worker started | pid=%s parent=%s email_delivery=%s",
                           os.getpid(), os.getppid(), settings.email_delivery)


@signals.worker_shutting_down.connect
def _log_worker_stopping(sig=None, how=None, exitcode=None, **_):
    _lifecycle_log.warning("AIRS worker stopping | pid=%s signal=%s how=%s exitcode=%s",
                           os.getpid(), sig, how, exitcode)
