"""
Batch 4 follow-up: emails stuck in SENDING over 30 minutes show in the monitoring panel and reach
HR_ADMIN in the stuck-send alert - counts and trigger types only.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.models.email import EmailTriggerEvent
from app.services.notifications.stuck_sends import STUCK_SENDING_AFTER, should_alert, stuck_sends

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
REJECTED = EmailTriggerEvent.CANDIDATE_REJECTED
SCHEDULED = EmailTriggerEvent.INTERVIEW_SCHEDULED


def _stuck(count=2, newest_minutes_ago=45):
    newest = NOW - timedelta(minutes=newest_minutes_ago)
    return {"count": count, "oldest_since": newest - timedelta(minutes=30), "newest_since": newest,
            "by_trigger": [], "stuck_after_minutes": 30}


def test_stuck_is_thirty_minutes():
    assert STUCK_SENDING_AFTER == timedelta(minutes=30)


def test_the_summary_counts_by_trigger_and_asks_for_rows_older_than_thirty_minutes():
    repo = MagicMock()
    old, older = NOW - timedelta(minutes=40), NOW - timedelta(minutes=90)
    repo.get_stuck_sending.return_value = [(SCHEDULED, 1, old, old), (REJECTED, 3, older, old)]
    with patch("app.services.notifications.stuck_sends.EmailNotificationRepository", return_value=repo):
        summary = stuck_sends(MagicMock(), now=NOW)
    assert repo.get_stuck_sending.call_args.args[0] == NOW - timedelta(minutes=30)
    assert repo.get_stuck_sending.call_args.kwargs["exclude_triggers"] == ()
    assert summary == {"count": 4, "oldest_since": older, "newest_since": old, "stuck_after_minutes": 30,
                       "by_trigger": [{"trigger_event": "CANDIDATE_REJECTED", "count": 3},
                                      {"trigger_event": "INTERVIEW_SCHEDULED", "count": 1}]}


def test_the_alert_does_not_count_its_own_emails():
    repo = MagicMock()
    repo.get_stuck_sending.return_value = []
    with patch("app.services.notifications.stuck_sends.EmailNotificationRepository", return_value=repo):
        assert stuck_sends(MagicMock(), now=NOW, for_alert=True)["count"] == 0
    assert repo.get_stuck_sending.call_args.kwargs["exclude_triggers"] == (EmailTriggerEvent.EMAIL_SEND_STUCK_ALERT,)


@pytest.mark.parametrize("stuck, last_alert_minutes_ago, alert", [
    (_stuck(count=0), None, False),                       # nothing stuck
    (_stuck(), None, True),                               # first time
    (_stuck(newest_minutes_ago=45), 20, True),            # became stuck (15 min ago) after the last alert
    (_stuck(newest_minutes_ago=45), 10, False),           # already reported, under 24 h
    (_stuck(newest_minutes_ago=45), 24 * 60, True),       # still stuck a day later: a reminder
])
def test_when_the_alert_goes_out(stuck, last_alert_minutes_ago, alert):
    last = None if last_alert_minutes_ago is None else NOW - timedelta(minutes=last_alert_minutes_ago)
    assert should_alert(stuck, last, NOW, repeat_hours=24) is alert


class _Task:
    def __init__(self, stuck, last_alert_at=None, hr_admins=("hr",)):
        self.stuck, self.last_alert_at, self.hr_admins = stuck, last_alert_at, list(hr_admins)

    def run(self):
        from app.tasks import email_health_tasks as mod
        notifications = MagicMock()
        notifications.get_latest_created_at.return_value = self.last_alert_at
        users = MagicMock()
        users.get_active_by_role.return_value = [SimpleNamespace(id=u, email=f"{u}@example.invalid")
                                                 for u in self.hr_admins]
        task_log_service = MagicMock()
        with patch.object(mod, "SessionLocal", return_value=MagicMock()), \
                patch.object(mod, "stuck_sends", return_value=self.stuck) as summary, \
                patch.object(mod, "EmailNotificationRepository", return_value=notifications), \
                patch.object(mod, "UserRepository", return_value=users), \
                patch.object(mod, "ConfigRepository", return_value=MagicMock(
                    get_configs_by_keys=MagicMock(return_value={}))), \
                patch.object(mod, "CeleryTaskLogService", return_value=task_log_service), \
                patch.object(mod, "CeleryTaskLogRepository"), \
                patch.object(mod, "queue_internal_user_emails", return_value=len(self.hr_admins)) as queue:
            mod.monitor_stuck_sends()
        assert summary.call_args.kwargs["for_alert"] is True
        return queue, task_log_service


def test_stuck_emails_alert_every_hr_admin_with_counts_only():
    stuck = _stuck(count=3)
    stuck["by_trigger"] = [{"trigger_event": "CANDIDATE_REJECTED", "count": 3}]
    queue, log = _Task(stuck, hr_admins=("hr1", "hr2")).run()
    _, users, trigger, context = queue.call_args.args
    assert trigger == EmailTriggerEvent.EMAIL_SEND_STUCK_ALERT
    assert [u.id for u in users] == ["hr1", "hr2"]
    assert context["stuck_count"] == 3 and context["by_trigger"] == "CANDIDATE_REJECTED (3)"
    assert context["stuck_after_minutes"] == 30 and context["monitoring_link"].endswith("/ai-screening/dashboard")
    assert not {"recipient_email", "candidate_name", "recipient_name"} & set(context)
    log.mark_success.assert_called_once()


def test_nothing_stuck_sends_nothing():
    queue, log = _Task(_stuck(count=0)).run()
    queue.assert_not_called()
    log.mark_success.assert_called_once()


def test_an_already_reported_problem_is_not_emailed_again_within_the_day():
    real_now = datetime.now(timezone.utc)
    stuck = {**_stuck(), "newest_since": real_now - timedelta(minutes=200)}
    queue, _ = _Task(stuck, last_alert_at=real_now - timedelta(minutes=60)).run()
    queue.assert_not_called()


def test_the_check_is_scheduled_and_runs_once_at_a_time():
    from app.core.celery_app import celery_app
    entries = [e for e in celery_app.conf.beat_schedule.values() if e["task"] == "notifications.monitor_stuck_sends"]
    assert len(entries) == 1
    assert "app.tasks.email_health_tasks" in celery_app.conf.imports
    # once at a time: tests/core/test_single_run.py checks every beat task carries @single_run


def test_the_monitoring_panel_shows_stuck_emails():
    from app.services.monitoring import monitoring_service as mod
    oldest = NOW - timedelta(minutes=95)
    summary = {"count": 2, "oldest_since": oldest, "newest_since": oldest, "stuck_after_minutes": 30,
               "by_trigger": [{"trigger_event": "CANDIDATE_REJECTED", "count": 2}]}
    with patch.object(mod, "stuck_sends", return_value=summary) as call:
        panel = mod._stuck_emails(MagicMock(), NOW)
    assert call.call_args.kwargs == {"now": NOW}       # the panel shows every stuck email, alerts included
    assert panel == {"count": 2, "oldest_minutes": 95, "stuck_after_minutes": 30,
                     "by_trigger": [{"trigger_event": "CANDIDATE_REJECTED", "count": 2}]}
