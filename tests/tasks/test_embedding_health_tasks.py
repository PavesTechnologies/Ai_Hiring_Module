from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.enums.constants import ActionType
from app.models.async_tasks import TaskStatus
from app.models.campaigns import CampaignStatus
from app.models.config import CBState
from app.models.email import EmailTriggerEvent
from app.models.identity import UserRole

TASKS_MODULE = "app.tasks.embedding_health_tasks"


def _make_campaign(name="Backend Hiring", status=CampaignStatus.ACTIVE):
    return SimpleNamespace(id=uuid4(), name=name, status=status)


def _make_hr_admin(email="admin@example.com"):
    return SimpleNamespace(id=str(uuid4()), email=email, role=UserRole.HR_ADMIN, is_active=True)


class _Harness:
    def __init__(self):
        self.campaign_repo = MagicMock()
        self.campaign_repo.get_all_campaigns.return_value = []
        self.campaign_candidate_repo = MagicMock()
        self.config_repo = MagicMock()
        self.config_repo.get_configs_by_keys.return_value = {}
        self.cb_repo = MagicMock()
        self.cb_repo.get_by_service_name.return_value = None
        self.user_repo = MagicMock()
        self.user_repo.get_active_by_role.return_value = [_make_hr_admin()]
        self.audit_service_instance = MagicMock()
        # The alert-state audit lookups: (latest of SENT/SKIPPED/CLEARED, latest SENT) - none by default.
        self.audit_repo = MagicMock()
        self.audit_repo.get_latest_of.return_value = None
        self.task_log_repo = MagicMock()
        self.task_log_repo.get_by_task_id.return_value = None

        def _create(log):
            log.retry_count = getattr(log, "retry_count", 0) or 0
            return log

        self.task_log_repo.create.side_effect = _create
        self.task_log_repo.update.side_effect = lambda log: log
        self.queue = MagicMock(return_value=1)

    def __enter__(self):
        self._patches = [
            patch(f"{TASKS_MODULE}.SessionLocal", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.CampaignRepository", return_value=self.campaign_repo),
            patch(f"{TASKS_MODULE}.CampaignCandidateRepository", return_value=self.campaign_candidate_repo),
            patch(f"{TASKS_MODULE}.ConfigRepository", return_value=self.config_repo),
            patch(f"{TASKS_MODULE}.CircuitBreakerRepository", return_value=self.cb_repo),
            patch(f"{TASKS_MODULE}.UserRepository", return_value=self.user_repo),
            patch(f"{TASKS_MODULE}.AuditRepository", return_value=self.audit_repo),
            patch(f"{TASKS_MODULE}.AuditService", return_value=self.audit_service_instance),
            patch(f"{TASKS_MODULE}.CeleryTaskLogRepository", return_value=self.task_log_repo),
            patch(f"{TASKS_MODULE}.queue_internal_user_emails", self.queue),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


def test_no_alert_when_failure_percentage_below_threshold():
    from app.tasks.embedding_health_tasks import monitor_embedding_health

    with _Harness() as h:
        campaign = _make_campaign()
        h.campaign_repo.get_all_campaigns.return_value = [campaign]
        h.campaign_candidate_repo.get_screening_semantic_health_stats.return_value = (1, 20)  # 5%
        h.config_repo.get_configs_by_keys.return_value = {"EMBEDDING_FAILURE_ALERT_THRESHOLD": "20.00"}

        monitor_embedding_health()

        h.queue.assert_not_called()
        h.audit_service_instance.log.assert_not_called()
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.SUCCESS


def test_alert_sent_when_failure_percentage_exceeds_threshold():
    from app.tasks.embedding_health_tasks import monitor_embedding_health

    with _Harness() as h:
        campaign = _make_campaign(name="Frontend Hiring")
        h.campaign_repo.get_all_campaigns.return_value = [campaign]
        h.campaign_candidate_repo.get_screening_semantic_health_stats.return_value = (10, 20)  # 50%
        h.config_repo.get_configs_by_keys.return_value = {"EMBEDDING_FAILURE_ALERT_THRESHOLD": "20.00"}

        monitor_embedding_health()

        # through the notifications path - recorded, governed by EMAIL_DELIVERY - never SES directly
        h.queue.assert_called_once()
        _db, users, trigger, context = h.queue.call_args.args
        assert [u.email for u in users] == ["admin@example.com"]
        assert trigger == EmailTriggerEvent.EMBEDDING_HEALTH_ALERT
        assert context["campaign_name"] == "Frontend Hiring" and context["failure_percentage"] == "50.00"
        assert context["affected_count"] == 10 and context["total_screening_count"] == 20
        assert context["suspended_note"] == ""

        h.audit_service_instance.log.assert_called_once()
        audit_kwargs = h.audit_service_instance.log.call_args.kwargs
        assert audit_kwargs["action_type"] == ActionType.PLATFORM_ALERT_SENT
        assert audit_kwargs["details"]["affected_count"] == 10
        assert audit_kwargs["details"]["total_screening_count"] == 20
        assert audit_kwargs["details"]["emails_queued"] == 1


def test_alert_mentions_suspended_when_circuit_breaker_open():
    from app.tasks.embedding_health_tasks import monitor_embedding_health

    with _Harness() as h:
        campaign = _make_campaign()
        h.campaign_repo.get_all_campaigns.return_value = [campaign]
        h.campaign_candidate_repo.get_screening_semantic_health_stats.return_value = (10, 20)
        h.cb_repo.get_by_service_name.return_value = SimpleNamespace(state=CBState.OPEN)

        monitor_embedding_health()

        assert "SUSPENDED" in h.queue.call_args.args[3]["suspended_note"]
        audit_kwargs = h.audit_service_instance.log.call_args.kwargs
        assert audit_kwargs["details"]["circuit_breaker_state"] == "OPEN"


def test_no_alert_when_no_screening_candidates_at_all():
    """Zero SCREENING candidates means a zero-denominator - must skip cleanly, never divide by zero."""
    from app.tasks.embedding_health_tasks import monitor_embedding_health

    with _Harness() as h:
        campaign = _make_campaign()
        h.campaign_repo.get_all_campaigns.return_value = [campaign]
        h.campaign_candidate_repo.get_screening_semantic_health_stats.return_value = (0, 0)

        monitor_embedding_health()

        h.queue.assert_not_called()
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.SUCCESS


def test_closed_campaigns_are_never_checked():
    from app.tasks.embedding_health_tasks import monitor_embedding_health

    with _Harness() as h:
        h.campaign_repo.get_all_campaigns.return_value = [_make_campaign(status=CampaignStatus.CLOSED)]

        monitor_embedding_health()

        h.campaign_candidate_repo.get_screening_semantic_health_stats.assert_not_called()


def test_no_active_hr_admin_skips_email_but_still_completes():
    from app.tasks.embedding_health_tasks import monitor_embedding_health

    with _Harness() as h:
        campaign = _make_campaign()
        h.campaign_repo.get_all_campaigns.return_value = [campaign]
        h.campaign_candidate_repo.get_screening_semantic_health_stats.return_value = (10, 20)
        h.user_repo.get_active_by_role.return_value = []

        monitor_embedding_health()

        h.queue.assert_not_called()
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.SUCCESS


def test_task_marks_failure_on_unexpected_exception():
    from app.tasks.embedding_health_tasks import monitor_embedding_health

    with _Harness() as h:
        h.campaign_repo.get_all_campaigns.side_effect = RuntimeError("db down")

        monitor_embedding_health()

        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.FAILURE


def test_the_task_has_no_direct_ses_path():
    import app.tasks.embedding_health_tasks as tasks
    assert not hasattr(tasks, "SESEmailClient")


# --- cooldown -----------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402

import pytest  # noqa: E402

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def _entry(action, hours_ago):
    return SimpleNamespace(action_type=action, created_at=NOW - timedelta(hours=hours_ago))


@pytest.mark.parametrize("over, latest, sent_hours_ago, expected", [
    (True, None, None, "send"),                                            # first crossing
    (True, ActionType.PLATFORM_ALERT_CLEARED, 2, "send"),                  # crossed again after recovering
    (True, ActionType.PLATFORM_ALERT_SENT, 1, "skip"),                     # still over, under 24 h
    (True, ActionType.PLATFORM_ALERT_SKIPPED, 23.9, "skip"),
    (True, ActionType.PLATFORM_ALERT_SKIPPED, 24, "send"),                 # still over 24 h later
    (True, ActionType.PLATFORM_ALERT_SENT, 30, "send"),
    (False, ActionType.PLATFORM_ALERT_SENT, 1, "clear"),                   # recovered
    (False, ActionType.PLATFORM_ALERT_SKIPPED, 1, "clear"),
    (False, ActionType.PLATFORM_ALERT_CLEARED, 1, None),                   # already recovered
    (False, None, None, None),
])
def test_alert_decision(over, latest, sent_hours_ago, expected):
    from app.tasks.embedding_health_tasks import alert_decision
    latest_entry = _entry(latest, 0.5) if latest else None
    last_sent_at = NOW - timedelta(hours=sent_hours_ago) if sent_hours_ago is not None else None
    assert alert_decision(over, latest_entry, last_sent_at, NOW, 24) == expected


def _run_over_threshold(h, latest, last_sent):
    from app.tasks.embedding_health_tasks import monitor_embedding_health
    campaign = _make_campaign()
    h.campaign_repo.get_all_campaigns.return_value = [campaign]
    h.campaign_candidate_repo.get_screening_semantic_health_stats.return_value = (10, 20)   # 50%
    h.audit_repo.get_latest_of.side_effect = [latest, last_sent]
    monitor_embedding_health()
    return campaign


def test_a_repeat_within_24_hours_is_skipped_and_audited():
    with _Harness() as h:
        sent = SimpleNamespace(action_type=ActionType.PLATFORM_ALERT_SENT,
                               created_at=datetime.now(timezone.utc) - timedelta(hours=3))
        _run_over_threshold(h, sent, sent)
        h.queue.assert_not_called()
        audit = h.audit_service_instance.log.call_args.kwargs
        assert audit["action_type"] == ActionType.PLATFORM_ALERT_SKIPPED
        assert audit["details"]["last_sent_at"] == sent.created_at.isoformat()
        assert audit["details"]["next_send_after"] == (sent.created_at + timedelta(hours=24)).isoformat()
        assert "emails_queued" not in audit["details"]
        summary = h.task_log_repo.update.call_args.args[0].output_summary
        assert '"alerts_skipped": 1' in summary and '"alerts_raised": 0' in summary


def test_still_over_24_hours_later_sends_again():
    with _Harness() as h:
        old = SimpleNamespace(action_type=ActionType.PLATFORM_ALERT_SKIPPED,
                              created_at=datetime.now(timezone.utc) - timedelta(minutes=30))
        sent = SimpleNamespace(action_type=ActionType.PLATFORM_ALERT_SENT,
                               created_at=datetime.now(timezone.utc) - timedelta(hours=24, minutes=5))
        _run_over_threshold(h, old, sent)
        h.queue.assert_called_once()
        assert h.audit_service_instance.log.call_args.kwargs["action_type"] == ActionType.PLATFORM_ALERT_SENT


def test_a_recovered_campaign_is_cleared_then_a_new_crossing_sends_at_once():
    from app.tasks.embedding_health_tasks import monitor_embedding_health
    with _Harness() as h:
        campaign = _make_campaign()
        h.campaign_repo.get_all_campaigns.return_value = [campaign]
        h.campaign_candidate_repo.get_screening_semantic_health_stats.return_value = (1, 20)    # 5%: recovered
        sent = SimpleNamespace(action_type=ActionType.PLATFORM_ALERT_SENT,
                               created_at=datetime.now(timezone.utc) - timedelta(hours=2))
        h.audit_repo.get_latest_of.side_effect = [sent, sent]
        monitor_embedding_health()
        h.queue.assert_not_called()
        assert h.audit_service_instance.log.call_args.kwargs["action_type"] == ActionType.PLATFORM_ALERT_CLEARED

    with _Harness() as h:
        cleared = SimpleNamespace(action_type=ActionType.PLATFORM_ALERT_CLEARED,
                                  created_at=datetime.now(timezone.utc) - timedelta(minutes=30))
        _run_over_threshold(h, cleared, sent)                       # the last email was only 2.5 h ago
        h.queue.assert_called_once()
        assert h.audit_service_instance.log.call_args.kwargs["action_type"] == ActionType.PLATFORM_ALERT_SENT


def test_the_repeat_interval_is_a_setting():
    with _Harness() as h:
        h.config_repo.get_configs_by_keys.return_value = {"EMBEDDING_FAILURE_ALERT_REPEAT_HOURS": "2"}
        sent = SimpleNamespace(action_type=ActionType.PLATFORM_ALERT_SENT,
                               created_at=datetime.now(timezone.utc) - timedelta(hours=3))
        _run_over_threshold(h, sent, sent)
        h.queue.assert_called_once()
