"""
The scheduled jobs (Celery beat), read-only on the Settings page (decision 2026-11-11: schedules are
view-only; editing them needs a database-backed scheduler). Read from the beat configuration itself, so the
list can't drift from what runs.
"""
from datetime import timedelta

from celery.schedules import crontab

_LABELS = {
    "auto-close-expired-campaigns": "Close campaigns past their deadline",
    "detect-duplicate-skill-aliases": "Find duplicate skill aliases",
    "evaluate-campaign-health-alerts": "Campaign health alerts",
    "detect-stalled-candidates": "Find stalled candidates (SLAs)",
    "evaluate-resubmission-alerts": "Repeat submission alerts",
    "expire-upload-decisions": "Expire pending upload decisions",
    "validate-talent-pool-eligibility": "Check Talent Pool eligibility",
    "retry-dead-lettered-erasure-file-deletes": "Retry failed erasure file deletes",
    "recover-pending-semantic-scores": "Recover pending semantic scores",
    "monitor-embedding-health": "Embedding health check",
    "monitor-stuck-email-sends": "Stuck email check",
    "complete-full-scoring": "Finish scoring held by the daily LLM limit",
    "recover-undecided-candidates": "Recover undecided candidates",
    "recover-stalled-resume-uploads": "Recover stalled resume uploads",
    "request-interview-feedback-for-ended-rounds": "Ask for interview feedback",
}


def _field(value) -> str:
    return str(value).strip()


def describe(schedule) -> str:
    """Plain words for the common shapes; the cron fields otherwise."""
    if isinstance(schedule, (int, float)):
        schedule = timedelta(seconds=schedule)
    if isinstance(schedule, timedelta):
        minutes = int(schedule.total_seconds() // 60)
        return f"Every {minutes} minutes" if minutes % 60 else f"Every {minutes // 60} hours"
    if isinstance(schedule, crontab):
        minute, hour = _field(schedule._orig_minute), _field(schedule._orig_hour)
        rest = [_field(schedule._orig_day_of_month), _field(schedule._orig_month_of_year),
                _field(schedule._orig_day_of_week)]
        if rest == ["*", "*", "*"]:
            if minute.isdigit() and hour.isdigit():
                return f"Daily at {int(hour):02d}:{int(minute):02d}"
            if minute.isdigit() and hour == "*":
                return f"Hourly at :{int(minute):02d}"
            if minute.isdigit() and hour.startswith("*/"):
                every = int(hour[2:])
                return "Hourly at :%02d" % int(minute) if every == 1 else f"Every {every} hours at :{int(minute):02d}"
            if minute.startswith("*/") and hour == "*":
                return f"Every {int(minute[2:])} minutes"
            start, _, step = minute.partition("-59/")
            if step and start.isdigit() and step.isdigit() and hour == "*":
                return f"Every {int(step)} minutes from :{int(start):02d}"
        return f"Cron: {minute} {hour} {' '.join(rest)}"
    return str(schedule)


def list_schedules(celery_app) -> dict:
    jobs = [{"name": name, "label": _LABELS.get(name, name), "task": entry["task"],
             "when": describe(entry["schedule"])}
            for name, entry in (celery_app.conf.beat_schedule or {}).items()]
    return {"timezone": str(celery_app.conf.timezone or "UTC"), "jobs": jobs,
            "note": "Read-only. The deadline check interval (DEADLINE_CHECK_INTERVAL_HOURS) applies only after "
                    "the scheduler restarts."}
