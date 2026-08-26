from unittest.mock import MagicMock, patch

from app.models.email import EmailTriggerEvent
from app.services.notifications.user_notification_preferences import is_notification_enabled

MODULE = "app.services.notifications.user_notification_preferences"

"""
Epic 5 Step 3 - is_notification_enabled(), built ahead of any real
caller (see the module's own docstring). Tested directly here even
though nothing calls it yet - the whole point of building it now is that
it's already correct and ready when a real caller arrives.
"""


def test_returns_true_when_no_preference_row_exists():
    """Default-on: an unlisted (user_id, trigger_event) pair is enabled."""
    repo = MagicMock()
    repo.get_by_user_id_and_trigger_event.return_value = None
    with patch(f"{MODULE}.UserNotificationPreferenceRepository", return_value=repo):
        assert is_notification_enabled(MagicMock(), "user-1", EmailTriggerEvent.UPLOAD_PERMANENTLY_FAILED) is True


def test_returns_false_when_an_explicit_opt_out_row_exists():
    repo = MagicMock()
    repo.get_by_user_id_and_trigger_event.return_value = MagicMock(is_enabled=False)
    with patch(f"{MODULE}.UserNotificationPreferenceRepository", return_value=repo):
        assert is_notification_enabled(MagicMock(), "user-1", EmailTriggerEvent.UPLOAD_PERMANENTLY_FAILED) is False


def test_returns_true_when_an_explicit_opt_in_row_exists():
    repo = MagicMock()
    repo.get_by_user_id_and_trigger_event.return_value = MagicMock(is_enabled=True)
    with patch(f"{MODULE}.UserNotificationPreferenceRepository", return_value=repo):
        assert is_notification_enabled(MagicMock(), "user-1", EmailTriggerEvent.UPLOAD_PERMANENTLY_FAILED) is True


def test_looks_up_by_the_exact_user_and_trigger_event_given():
    repo = MagicMock()
    repo.get_by_user_id_and_trigger_event.return_value = None
    with patch(f"{MODULE}.UserNotificationPreferenceRepository", return_value=repo):
        is_notification_enabled(MagicMock(), "user-42", EmailTriggerEvent.CANDIDATE_SELECTED)

    repo.get_by_user_id_and_trigger_event.assert_called_once_with("user-42", EmailTriggerEvent.CANDIDATE_SELECTED)
