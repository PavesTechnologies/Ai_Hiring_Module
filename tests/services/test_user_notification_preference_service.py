from unittest.mock import MagicMock

from app.models.email import EmailTriggerEvent
from app.services.user_notification_preference_service import UserNotificationPreferenceService

"""
Epic 5 Step 3 - the minimal read/write service backing GET/PUT
/users/me/notification-preferences.
"""


def _pref(trigger_event, is_enabled):
    return MagicMock(trigger_event=trigger_event, is_enabled=is_enabled)


def test_get_preferences_returns_every_known_trigger_event():
    repo = MagicMock()
    repo.get_all_by_user_id.return_value = []
    service = UserNotificationPreferenceService(repo)

    result = service.get_preferences("user-1")

    assert {r.trigger_event for r in result} == {e.value for e in EmailTriggerEvent}
    assert all(r.is_enabled is True for r in result)  # default-on, nothing explicit


def test_get_preferences_applies_explicit_opt_outs_on_top_of_the_default():
    repo = MagicMock()
    repo.get_all_by_user_id.return_value = [_pref(EmailTriggerEvent.CANDIDATE_SELECTED, False)]
    service = UserNotificationPreferenceService(repo)

    result = service.get_preferences("user-1")

    by_event = {r.trigger_event: r.is_enabled for r in result}
    assert by_event[EmailTriggerEvent.CANDIDATE_SELECTED.value] is False
    assert by_event[EmailTriggerEvent.CANDIDATE_REJECTED.value] is True  # untouched, still default-on


def test_set_preference_upserts_and_commits():
    repo = MagicMock()
    repo.upsert.return_value = _pref(EmailTriggerEvent.INTERVIEW_SCHEDULED, False)
    service = UserNotificationPreferenceService(repo)

    result = service.set_preference("user-1", EmailTriggerEvent.INTERVIEW_SCHEDULED, False)

    repo.upsert.assert_called_once_with("user-1", EmailTriggerEvent.INTERVIEW_SCHEDULED, False)
    repo.commit.assert_called_once()
    assert result.trigger_event == EmailTriggerEvent.INTERVIEW_SCHEDULED.value
    assert result.is_enabled is False
