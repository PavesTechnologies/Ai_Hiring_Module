"""Settings phase 1: alert recipients (every active HR admin, opt-out per alert) and the read-only schedules."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from celery.schedules import crontab

from app.exceptions.campaign_exceptions import CampaignException
from app.services.settings.alert_settings import ALERTS, AlertSettingsService
from app.services.settings.schedules import _LABELS, describe, list_schedules

MODULE = "app.services.settings.alert_settings"


def _service(admins, prefs=None):
    prefs = dict(prefs or {})
    users, preferences = MagicMock(), MagicMock()
    users.get_active_by_role.return_value = admins
    preferences.get_by_user_id_and_trigger_event.side_effect = \
        lambda uid, trig: (SimpleNamespace(is_enabled=prefs[(uid, trig.value)]) if (uid, trig.value) in prefs else None)
    audit = MagicMock()
    with patch(f"{MODULE}.UserRepository", return_value=users), \
            patch(f"{MODULE}.UserNotificationPreferenceRepository", return_value=preferences):
        service = AlertSettingsService(MagicMock(), audit)
    return service, preferences, audit


_ANN = SimpleNamespace(id="u1", full_name="Ann", email="ann@example.invalid")
_BOB = SimpleNamespace(id="u2", full_name="Bob", email="")


def test_every_active_hr_admin_receives_unless_opted_out():
    service, _, _ = _service([_BOB, _ANN], {("u1", "EMBEDDING_HEALTH_ALERT"): False})
    alerts = {a["trigger_event"]: a for a in service.list()}
    stuck = alerts["EMAIL_SEND_STUCK_ALERT"]
    assert [r["name"] for r in stuck["recipients"]] == ["Ann", "Bob"]
    assert [r["enabled"] for r in stuck["recipients"]] == [True, True]
    assert stuck["receiving"] == 1                                    # Bob has no email address
    assert alerts["EMBEDDING_HEALTH_ALERT"]["recipients"][0]["enabled"] is False
    assert alerts["EMBEDDING_HEALTH_ALERT"]["receiving"] == 0


def test_switching_a_recipient_off_is_saved_and_audited_with_ids_only():
    service, preferences, audit = _service([_ANN])
    service.set_recipient("EMAIL_SEND_STUCK_ALERT", "u1", False, actor_id="hr-9")
    preferences.upsert.assert_called_once()
    assert preferences.upsert.call_args.args[0] == "u1" and preferences.upsert.call_args.args[2] is False
    details = audit.log.call_args.kwargs["details"]
    assert details == {"title": "Alert recipient changed", "trigger_event": "EMAIL_SEND_STUCK_ALERT",
                       "recipient_user_id": "u1", "enabled": {"before": True, "after": False}}
    assert "ann" not in str(details).lower()
    preferences.commit.assert_called_once()


def test_no_change_writes_nothing():
    service, preferences, audit = _service([_ANN])
    service.set_recipient("EMAIL_SEND_STUCK_ALERT", "u1", True, actor_id="hr-9")
    preferences.upsert.assert_not_called()
    audit.log.assert_not_called()


@pytest.mark.parametrize("trigger, user_id, enabled, status", [
    ("CANDIDATE_REJECTED", "u1", False, 404),       # not an internal alert
    ("EMAIL_SEND_STUCK_ALERT", "u1", "no", 422),
    ("EMAIL_SEND_STUCK_ALERT", "someone-else", False, 422),
])
def test_bad_requests_are_refused(trigger, user_id, enabled, status):
    service, preferences, _ = _service([_ANN])
    with pytest.raises(CampaignException) as exc:
        service.set_recipient(trigger, user_id, enabled, actor_id="hr-9")
    assert exc.value.status_code == status
    preferences.upsert.assert_not_called()


def test_the_listed_alerts_are_the_ones_sent_to_hr_admins():
    assert {a["trigger_event"].value for a in ALERTS} == {"EMAIL_SEND_STUCK_ALERT", "EMBEDDING_HEALTH_ALERT"}


@pytest.mark.parametrize("schedule, words", [
    (crontab(minute=0, hour=2), "Daily at 02:00"),
    (crontab(minute=40), "Hourly at :40"),
    (crontab(minute=0, hour="*/1"), "Hourly at :00"),
    (crontab(minute=5, hour="*/6"), "Every 6 hours at :05"),
    (crontab(minute="*/15"), "Every 15 minutes"),
    (crontab(minute="7-59/15"), "Every 15 minutes from :07"),
    (timedelta(minutes=30), "Every 30 minutes"),
    (300.0, "Every 5 minutes"),
    (crontab(minute=0, hour=3, day_of_week="mon"), "Cron: 0 3 * * mon"),
])
def test_schedules_are_described_in_plain_words(schedule, words):
    assert describe(schedule) == words


def test_every_scheduled_job_is_listed_with_a_label():
    from app.core.celery_app import celery_app
    result = list_schedules(celery_app)
    assert {j["name"] for j in result["jobs"]} == set(celery_app.conf.beat_schedule)
    assert all(j["name"] in _LABELS for j in result["jobs"]), "label the new job in schedules._LABELS"
